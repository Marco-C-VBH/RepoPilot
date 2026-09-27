"""Thin, provider-agnostic model clients.

One method matters: ``complete(messages, tools=...) -> ModelResponse``.  The two
adapters (Anthropic Messages API, OpenAI Responses API) translate the neutral
types in ``repopilot.models.types`` to the vendor SDKs and back; ``FakeClient``
plays scripted responses so the agent, the runner and the tests never need a
network or a key.

Deliberately *not* here: prompt templates, retries beyond what the SDKs do
themselves (both retry 429s, 5xxs and connection errors with backoff), and any
notion of agent state.  Streaming is out of scope for a batch benchmark.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from repopilot.models.config import Provider, api_key_for, provider_for
from repopilot.models.pricing import estimate_cost
from repopilot.models.types import (
    Message,
    ModelResponse,
    Role,
    StopReason,
    ToolCall,
    ToolSpec,
    Usage,
)

DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_SDK_RETRIES = 3


class ModelError(RuntimeError):
    """A call failed after the SDK's own retries (auth, quota, network, bad request)."""

    def __init__(self, provider: str, model: str, cause: BaseException) -> None:
        self.provider = provider
        self.model = model
        self.cause = cause
        super().__init__(f"{provider} call to {model} failed: {type(cause).__name__}: {cause}")


class ModelClient(Protocol):
    model: str
    provider: str

    def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float | None = None,
        tool_choice: str = "auto",
    ) -> ModelResponse: ...


def _split_system(messages: Sequence[Message]) -> tuple[str | None, list[Message]]:
    """Pull the single leading system message out; reject misplaced ones."""
    if not messages:
        raise ValueError("a completion needs at least one message")
    system: str | None = None
    rest = list(messages)
    if rest[0].role is Role.SYSTEM:
        system = rest.pop(0).content
    for m in rest:
        if m.role is Role.SYSTEM:
            raise ValueError("only one system message is allowed, and it must come first")
    if not rest:
        raise ValueError("a completion needs a user message after the system prompt")
    return system, rest


def _timed(call: Callable[[], Any]) -> tuple[Any, int]:
    start = time.perf_counter()
    result = call()
    return result, int((time.perf_counter() - start) * 1000)


# --------------------------------------------------------------------------- Anthropic


class AnthropicClient:
    provider = str(Provider.ANTHROPIC)

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        sdk_client: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_SDK_RETRIES,
    ) -> None:
        self.model = model
        if sdk_client is None:
            key = api_key or api_key_for(Provider.ANTHROPIC)  # clear error before the import
            import anthropic  # imported lazily so the package is optional for tests

            sdk_client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=max_retries)
        self._sdk = sdk_client

    # -- request ------------------------------------------------------------------
    @staticmethod
    def to_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
        """Neutral turns -> Anthropic ``messages``.  Consecutive tool results become a
        single user message of ``tool_result`` blocks, as the API requires, and a
        user turn that follows tool results (the runtime's phase instructions) is
        appended to that same message as a text block."""
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role is Role.USER:
                if out and out[-1]["role"] == "user":
                    previous = out[-1]["content"]
                    if isinstance(previous, str):
                        previous = [{"type": "text", "text": previous}]
                    out[-1]["content"] = [*previous, {"type": "text", "text": m.content}]
                else:
                    out.append({"role": "user", "content": m.content})
            elif m.role is Role.ASSISTANT:
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for call in m.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                    )
                out.append({"role": "assistant", "content": blocks or m.content})
            elif m.role is Role.TOOL:
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id,
                    "content": m.content,
                }
                if m.is_error:
                    block["is_error"] = True
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            else:  # pragma: no cover - _split_system removes system turns first
                raise ValueError(f"unexpected role {m.role}")
        return out

    @staticmethod
    def to_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]

    def build_request(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float | None = None,
        tool_choice: str = "auto",
    ) -> dict[str, Any]:
        system, rest = _split_system(messages)
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": self.to_messages(rest),
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = self.to_tools(tools)
            if tool_choice == "none":
                # The history may hold tool_use blocks, which the API only accepts
                # when tools are defined; "none" keeps them defined but unusable.
                request["tool_choice"] = {"type": "none"}
        # `temperature` is accepted for interface symmetry only: the Messages API
        # of the installed SDK (anthropic 1.x) has no sampling temperature, and
        # `Messages.create` rejects the keyword outright (issue #15). Callers that
        # want a deterministic probe get the provider's default sampling here.
        return request

    # -- response -----------------------------------------------------------------
    _STOP = {
        "end_turn": StopReason.END_TURN,
        "stop_sequence": StopReason.END_TURN,
        "tool_use": StopReason.TOOL_USE,
        "max_tokens": StopReason.MAX_TOKENS,
    }

    def parse_response(self, raw: Any, latency_ms: int) -> ModelResponse:
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in getattr(raw, "content", None) or []:
            kind = getattr(block, "type", None)
            if kind == "text":
                texts.append(block.text)
            elif kind == "tool_use":
                arguments = block.input if isinstance(block.input, dict) else {}
                error = None if isinstance(block.input, dict) else "tool input was not an object"
                calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=arguments, parse_error=error)
                )
        u = getattr(raw, "usage", None)
        cache_read = getattr(u, "cache_read_input_tokens", None) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", None) or 0
        # Anthropic reports uncached, cache-read and cache-written input separately;
        # Usage.input_tokens is the whole prompt.
        usage = Usage(
            input_tokens=(getattr(u, "input_tokens", 0) or 0) + cache_read + cache_write,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        model = getattr(raw, "model", None) or self.model
        return ModelResponse(
            model=model,
            provider=self.provider,
            text="".join(texts),
            tool_calls=tuple(calls),
            stop_reason=self._STOP.get(getattr(raw, "stop_reason", None), StopReason.OTHER),
            usage=usage,
            latency_ms=latency_ms,
            cost_usd=estimate_cost(self.model, usage),
            response_id=getattr(raw, "id", None),
        )

    def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float | None = None,
        tool_choice: str = "auto",
    ) -> ModelResponse:
        request = self.build_request(
            messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            tool_choice=tool_choice,
        )
        try:
            raw, latency_ms = _timed(lambda: self._sdk.messages.create(**request))
        except Exception as exc:
            raise ModelError(self.provider, self.model, exc) from exc
        return self.parse_response(raw, latency_ms)


# ------------------------------------------------------------------------------ OpenAI


def _item_to_dict(item: Any) -> dict[str, Any]:
    """An SDK output item (pydantic model) or a plain dict -> plain dict."""
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        return dump(exclude_none=True, mode="json")
    return dict(vars(item))


class OpenAIClient:
    """The Responses API with function tools.

    Chat Completions cannot combine function tools with the reasoning that the
    gpt-5.x models do by default ("use /v1/responses or set reasoning_effort to
    'none'"), so the adapter speaks Responses.  The output items of each turn
    (reasoning, message, function_call) are handed back through
    ``ModelResponse.raw_items`` and replayed verbatim on the next call, which is
    how the API carries reasoning state across tool calls without server-side
    storage (``store=False``).
    """

    provider = str(Provider.OPENAI)

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        sdk_client: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_SDK_RETRIES,
        reasoning_effort: str | None = None,
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort  # None -> the model's default
        if sdk_client is None:
            key = api_key or api_key_for(Provider.OPENAI)  # clear error before the import
            import openai  # imported lazily so the package is optional for tests

            sdk_client = openai.OpenAI(api_key=key, timeout=timeout, max_retries=max_retries)
        self._sdk = sdk_client

    # -- request ------------------------------------------------------------------
    @staticmethod
    def to_input(messages: Sequence[Message]) -> list[dict[str, Any]]:
        """Neutral turns -> Responses ``input`` items (the system prompt travels as
        ``instructions``, not here)."""
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role is Role.USER:
                out.append({"role": "user", "content": m.content})
            elif m.role is Role.ASSISTANT:
                if m.raw_items:  # replay the provider's own items, reasoning included
                    out.extend(m.raw_items)
                    continue
                if m.content:
                    out.append({"role": "assistant", "content": m.content})
                for c in m.tool_calls:
                    out.append(
                        {
                            "type": "function_call",
                            "call_id": c.id,
                            "name": c.name,
                            "arguments": json.dumps(c.arguments),
                        }
                    )
            elif m.role is Role.TOOL:
                out.append(
                    {"type": "function_call_output", "call_id": m.tool_call_id, "output": m.content}
                )
            else:  # pragma: no cover
                raise ValueError(f"unexpected role {m.role}")
        return out

    @staticmethod
    def to_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
        # strict=False: strict schemas must mark every property required and forbid
        # extras; our tool schemas are ordinary JSON Schema and invalid arguments
        # are counted rather than prevented.
        return [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "strict": False,
            }
            for t in tools
        ]

    def build_request(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float | None = None,
        tool_choice: str = "auto",
    ) -> dict[str, Any]:
        system, rest = _split_system(messages)
        request: dict[str, Any] = {
            "model": self.model,
            "input": self.to_input(rest),
            "max_output_tokens": max_tokens,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if system:
            request["instructions"] = system
        if tools:
            request["tools"] = self.to_tools(tools)
            request["tool_choice"] = "none" if tool_choice == "none" else "auto"
        if self.reasoning_effort is not None:
            request["reasoning"] = {"effort": self.reasoning_effort}
        # Reasoning models reject non-default temperatures, so it is only sent
        # when explicitly asked for.
        if temperature is not None:
            request["temperature"] = temperature
        return request

    # -- response -----------------------------------------------------------------
    def parse_response(self, raw: Any, latency_ms: int) -> ModelResponse:
        status = getattr(raw, "status", None)
        if status == "failed":
            error = getattr(raw, "error", None)
            detail = getattr(error, "message", None) or str(error)
            raise ModelError(self.provider, self.model, RuntimeError(f"response failed: {detail}"))

        texts: list[str] = []
        refusals: list[str] = []
        calls: list[ToolCall] = []
        items: list[dict[str, Any]] = []
        for item in getattr(raw, "output", None) or []:
            items.append(_item_to_dict(item))
            kind = getattr(item, "type", None)
            if kind == "message":
                for part in getattr(item, "content", None) or []:
                    part_type = getattr(part, "type", None)
                    if part_type == "output_text":
                        texts.append(part.text)
                    elif part_type == "refusal":
                        refusals.append(getattr(part, "refusal", "") or "")
            elif kind == "function_call":
                calls.append(ToolCall.from_json_arguments(item.call_id, item.name, item.arguments))

        u = getattr(raw, "usage", None)
        in_details = getattr(u, "input_tokens_details", None)
        out_details = getattr(u, "output_tokens_details", None)
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,  # includes cached tokens
            output_tokens=getattr(u, "output_tokens", 0) or 0,  # includes reasoning tokens
            cache_read_tokens=getattr(in_details, "cached_tokens", None) or 0,
            reasoning_tokens=getattr(out_details, "reasoning_tokens", None) or 0,
        )

        if status == "incomplete":
            reason = getattr(getattr(raw, "incomplete_details", None), "reason", None)
            stop = StopReason.MAX_TOKENS if reason == "max_output_tokens" else StopReason.OTHER
        elif refusals and not texts and not calls:
            stop = StopReason.OTHER
        elif calls:
            stop = StopReason.TOOL_USE
        else:
            stop = StopReason.END_TURN

        model = getattr(raw, "model", None) or self.model
        return ModelResponse(
            model=model,
            provider=self.provider,
            text="".join(texts) or "".join(refusals),
            tool_calls=tuple(calls),
            stop_reason=stop,
            usage=usage,
            latency_ms=latency_ms,
            cost_usd=estimate_cost(self.model, usage),
            response_id=getattr(raw, "id", None),
            raw_items=tuple(items),
        )

    def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float | None = None,
        tool_choice: str = "auto",
    ) -> ModelResponse:
        request = self.build_request(
            messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            tool_choice=tool_choice,
        )
        try:
            raw, latency_ms = _timed(lambda: self._sdk.responses.create(**request))
        except Exception as exc:
            raise ModelError(self.provider, self.model, exc) from exc
        return self.parse_response(raw, latency_ms)


# -------------------------------------------------------------------------------- Fake


@dataclass
class RecordedCall:
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    max_tokens: int
    temperature: float | None
    tool_choice: str = "auto"


@dataclass
class FakeClient:
    """Plays back scripted responses and records every request.

    Scripts can be full ``ModelResponse`` objects or, for convenience, plain
    strings (a text reply) and tuples of ``ToolCall`` (a tool-use reply).  Usage
    is synthesized from the message sizes so cost accounting has something to
    chew on in tests.
    """

    model: str = "fake-model"
    provider: str = "fake"
    script: list[ModelResponse | str | tuple[ToolCall, ...]] = field(default_factory=list)
    calls: list[RecordedCall] = field(default_factory=list)
    price_per_token: float = 0.0

    def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float | None = None,
        tool_choice: str = "auto",
    ) -> ModelResponse:
        _split_system(messages)  # same validation as the real clients
        self.calls.append(
            RecordedCall(tuple(messages), tuple(tools), max_tokens, temperature, tool_choice)
        )
        if not self.script:
            raise ModelError(self.provider, self.model, RuntimeError("fake script exhausted"))
        item = self.script.pop(0)
        if isinstance(item, ModelResponse):
            return item
        input_tokens = sum(len(m.content) // 4 + 4 for m in messages)
        if isinstance(item, str):
            usage = Usage(input_tokens, len(item) // 4 + 1)
            return ModelResponse(
                model=self.model,
                provider=self.provider,
                text=item,
                tool_calls=(),
                stop_reason=StopReason.END_TURN,
                usage=usage,
                latency_ms=1,
                cost_usd=usage.total_tokens * self.price_per_token,
            )
        usage = Usage(input_tokens, 16 * len(item))
        return ModelResponse(
            model=self.model,
            provider=self.provider,
            text="",
            tool_calls=tuple(item),
            stop_reason=StopReason.TOOL_USE,
            usage=usage,
            latency_ms=1,
            cost_usd=usage.total_tokens * self.price_per_token,
        )


# ------------------------------------------------------------------------------ factory


def client_for(model: str, **kwargs: Any) -> ModelClient:
    """The right adapter for a model id; the key comes from the environment unless given."""
    provider = provider_for(model)
    if provider is Provider.ANTHROPIC:
        return AnthropicClient(model, **kwargs)
    return OpenAIClient(model, **kwargs)
