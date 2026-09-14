"""Provider-neutral request and response types for model calls.

The agent only ever sees these types.  Each provider adapter in
``repopilot.models.client`` translates them to and from its SDK's wire format,
so the runtime, the traces and the tests never depend on which vendor served a
call.  Everything here is a frozen dataclass: a ``ModelResponse`` is a fact
about one call and goes straight into the trace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class StopReason(StrEnum):
    """Why the model stopped, normalized across providers."""

    END_TURN = "end_turn"  # finished a normal reply
    TOOL_USE = "tool_use"  # wants tool results before continuing
    MAX_TOKENS = "max_tokens"  # cut off by ``max_tokens``; the reply is incomplete
    OTHER = "other"  # content filter, refusal, anything else the provider reports


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model.  ``parameters`` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False}
    )


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation requested by the model.

    ``arguments`` is the parsed argument object.  When a provider returns the
    arguments as text that is not valid JSON (OpenAI sends a JSON string, and
    models occasionally truncate it), ``arguments`` is empty and
    ``parse_error`` explains why; the runtime counts those as invalid tool
    calls (spec §11.1) instead of crashing.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    parse_error: str | None = None

    @classmethod
    def from_json_arguments(cls, id: str, name: str, raw: str | None) -> ToolCall:
        if raw is None or raw.strip() == "":
            return cls(id=id, name=name)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return cls(id=id, name=name, parse_error=f"arguments are not valid JSON: {exc}")
        if not isinstance(parsed, dict):
            return cls(
                id=id,
                name=name,
                parse_error=f"arguments must be a JSON object, got {type(parsed).__name__}",
            )
        return cls(id=id, name=name, arguments=parsed)


@dataclass(frozen=True)
class Message:
    """One turn of a conversation.

    * ``SYSTEM`` -- instructions; at most one, first.
    * ``USER`` / ``ASSISTANT`` -- plain text; an assistant turn may also carry
      the ``tool_calls`` it made, which must be answered by ``TOOL`` messages
      (one per call, in order) before the next assistant turn.
    * ``TOOL`` -- the result of one tool call: ``tool_call_id`` names the call,
      ``content`` is the textual result.
    """

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    is_error: bool = False  # TOOL only: the tool failed (providers can flag this)
    # ASSISTANT only: the provider's own output items for this turn, replayed verbatim
    # on the next call by adapters that need them (OpenAI's Responses API keeps its
    # reasoning state there).  Opaque to everything else; other providers ignore it.
    raw_items: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.tool_calls and self.role is not Role.ASSISTANT:
            raise ValueError("only assistant messages can carry tool_calls")
        if self.raw_items and self.role is not Role.ASSISTANT:
            raise ValueError("only assistant messages can carry raw_items")
        if self.role is Role.TOOL and not self.tool_call_id:
            raise ValueError("tool messages need the tool_call_id they answer")
        if self.tool_call_id and self.role is not Role.TOOL:
            raise ValueError("tool_call_id is only valid on tool messages")


def system(content: str) -> Message:
    return Message(Role.SYSTEM, content)


def user(content: str) -> Message:
    return Message(Role.USER, content)


def assistant(
    content: str = "",
    tool_calls: tuple[ToolCall, ...] = (),
    raw_items: tuple[dict[str, Any], ...] = (),
) -> Message:
    return Message(Role.ASSISTANT, content, tool_calls=tool_calls, raw_items=raw_items)


def tool_result(tool_call_id: str, content: str, *, is_error: bool = False) -> Message:
    return Message(Role.TOOL, content, tool_call_id=tool_call_id, is_error=is_error)


@dataclass(frozen=True)
class Usage:
    """Token counts for one call, as the provider reported them.

    ``input_tokens`` is the full prompt size including any cached part;
    ``cache_read_tokens`` / ``cache_write_tokens`` say how much of it was served
    from, or written to, a prompt cache (both providers bill those differently).
    ``reasoning_tokens`` is the share of ``output_tokens`` a reasoning model spent
    thinking; it is billed as output and never visible in ``text``.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0  # part of output_tokens spent on hidden reasoning

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )


@dataclass(frozen=True)
class ModelResponse:
    """The outcome of one model call: what it said, what it wants, what it cost."""

    model: str  # the model id the provider actually served
    provider: str
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop_reason: StopReason
    usage: Usage
    latency_ms: int
    cost_usd: float
    response_id: str | None = None
    raw_items: tuple[dict[str, Any], ...] = ()  # see Message.raw_items

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def as_message(self) -> Message:
        """The assistant turn to append to the conversation before tool results."""
        return Message(
            Role.ASSISTANT, self.text, tool_calls=self.tool_calls, raw_items=self.raw_items
        )

    def to_record(self) -> dict[str, Any]:
        """JSON-serializable summary for traces (spec §13.1: model, tokens, latency, cost)."""
        return {
            "model": self.model,
            "provider": self.provider,
            "stop_reason": str(self.stop_reason),
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_tokens": self.usage.cache_read_tokens,
            "cache_write_tokens": self.usage.cache_write_tokens,
            "reasoning_tokens": self.usage.reasoning_tokens,
            "latency_ms": self.latency_ms,
            "cost_usd": round(self.cost_usd, 6),
            "tool_calls": [c.name for c in self.tool_calls],
            "invalid_tool_calls": sum(1 for c in self.tool_calls if c.parse_error),
            "response_id": self.response_id,
        }
