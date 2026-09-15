"""The Anthropic and OpenAI adapters, driven with stand-in SDK clients.

Each fake records the keyword arguments the adapter would send and returns an
object shaped like the SDK's response (attribute access, same field names), so
the translation in both directions is checked without the SDKs installed, a
network, or a key.  ``tests/test_models_live.py`` does the one real round trip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from repopilot.models import (
    AnthropicClient,
    ModelError,
    OpenAIClient,
    StopReason,
    ToolCall,
    ToolSpec,
    Usage,
    assistant,
    client_for,
    estimate_cost,
    system,
    tool_result,
    user,
)

TOOLS = [
    ToolSpec(
        "read_file",
        "Read a line range",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
]
CONVERSATION = [
    system("You fix bugs."),
    user("The cache expires early."),
    assistant("Let me look.", tool_calls=(ToolCall("t1", "read_file", {"path": "a.py"}),)),
    tool_result("t1", "def f(): ...", is_error=False),
    assistant("", tool_calls=(ToolCall("t2", "read_file", {"path": "b.py"}),)),
    tool_result("t2", "no such file", is_error=True),
]


# ---------------------------------------------------------------------- Anthropic fake


@dataclass
class FakeAnthropicSDK:
    response: Any
    requests: list[dict[str, Any]] = field(default_factory=list)
    error: Exception | None = None

    @property
    def messages(self) -> Any:
        return self

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def anthropic_response(
    *blocks: Any, stop: str = "end_turn", usage: dict[str, int] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        id="msg_1",
        model="claude-sonnet-5-20260101",
        content=list(blocks),
        stop_reason=stop,
        usage=SimpleNamespace(**{"input_tokens": 100, "output_tokens": 20, **(usage or {})}),
    )


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_use_block(id: str, name: str, input: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def test_anthropic_request_translation() -> None:
    sdk = FakeAnthropicSDK(anthropic_response(text_block("ok")))
    client = AnthropicClient("claude-sonnet-5", sdk_client=sdk)
    client.complete(CONVERSATION, tools=TOOLS, max_tokens=512, temperature=0.0)

    (request,) = sdk.requests
    assert request["model"] == "claude-sonnet-5"
    assert request["system"] == "You fix bugs."
    assert request["max_tokens"] == 512
    assert request["temperature"] == 0.0
    assert request["tools"] == [
        {
            "name": "read_file",
            "description": "Read a line range",
            "input_schema": TOOLS[0].parameters,
        }
    ]
    assert request["messages"] == [
        {"role": "user", "content": "The cache expires early."},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.py"}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "def f(): ..."}],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t2", "name": "read_file", "input": {"path": "b.py"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t2",
                    "content": "no such file",
                    "is_error": True,
                }
            ],
        },
    ]


def test_anthropic_merges_consecutive_tool_results_into_one_user_turn() -> None:
    calls = (ToolCall("a", "read_file", {}), ToolCall("b", "read_file", {}))
    messages = [user("go"), assistant("", calls), tool_result("a", "1"), tool_result("b", "2")]
    payload = AnthropicClient.to_messages(messages)
    assert [m["role"] for m in payload] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in payload[-1]["content"]] == ["a", "b"]

    # A user turn right after tool results (the runtime's phase instructions) joins
    # that user message as a text block; two plain user turns merge the same way.
    payload = AnthropicClient.to_messages([*messages, user("Phase: PATCH")])
    assert [m["role"] for m in payload] == ["user", "assistant", "user"]
    assert payload[-1]["content"][-1] == {"type": "text", "text": "Phase: PATCH"}
    payload = AnthropicClient.to_messages([user("a"), user("b")])
    assert payload == [
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    ]


def test_anthropic_omits_temperature_and_tools_when_not_given() -> None:
    sdk = FakeAnthropicSDK(anthropic_response(text_block("ok")))
    AnthropicClient("claude-sonnet-5", sdk_client=sdk).complete([user("hi")])
    assert "temperature" not in sdk.requests[0]
    assert "tools" not in sdk.requests[0]
    assert "system" not in sdk.requests[0]


def test_anthropic_response_parsing_with_tool_use_and_cache_usage() -> None:
    raw = anthropic_response(
        text_block("I will read "),
        text_block("the file."),
        tool_use_block("t9", "read_file", {"path": "a.py"}),
        tool_use_block("t10", "read_file", "not-an-object"),
        stop="tool_use",
        usage={
            "input_tokens": 100,
            "output_tokens": 40,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 50,
        },
    )
    client = AnthropicClient("claude-sonnet-5", sdk_client=FakeAnthropicSDK(raw))
    response = client.complete([user("hi")])

    assert response.text == "I will read the file."
    assert response.stop_reason is StopReason.TOOL_USE
    assert response.tool_calls[0] == ToolCall("t9", "read_file", {"path": "a.py"})
    assert response.tool_calls[1].parse_error == "tool input was not an object"
    # Anthropic reports uncached / cached / written input separately; we total them.
    assert response.usage == Usage(450, 40, cache_read_tokens=300, cache_write_tokens=50)
    assert response.cost_usd == pytest.approx(estimate_cost("claude-sonnet-5", response.usage))
    assert response.model == "claude-sonnet-5-20260101"  # what the provider served
    assert response.provider == "anthropic"
    assert response.response_id == "msg_1"
    assert response.latency_ms >= 0


@pytest.mark.parametrize(
    "stop, expected",
    [
        ("end_turn", StopReason.END_TURN),
        ("stop_sequence", StopReason.END_TURN),
        ("max_tokens", StopReason.MAX_TOKENS),
        ("refusal", StopReason.OTHER),
        (None, StopReason.OTHER),
    ],
)
def test_anthropic_stop_reasons(stop: str | None, expected: StopReason) -> None:
    raw = anthropic_response(text_block("x"), stop=stop)
    client = AnthropicClient("claude-sonnet-5", sdk_client=FakeAnthropicSDK(raw))
    assert client.complete([user("hi")]).stop_reason is expected


def test_anthropic_sdk_errors_become_model_errors() -> None:
    sdk = FakeAnthropicSDK(None, error=RuntimeError("rate limited"))
    client = AnthropicClient("claude-sonnet-5", sdk_client=sdk)
    with pytest.raises(ModelError, match="anthropic call to claude-sonnet-5 failed: RuntimeError"):
        client.complete([user("hi")])


# ------------------------------------------------------------------------- OpenAI fake


@dataclass
class FakeOpenAISDK:
    response: Any
    requests: list[dict[str, Any]] = field(default_factory=list)
    error: Exception | None = None

    @property
    def responses(self) -> Any:
        return self

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class Item(SimpleNamespace):
    """An SDK output item: attribute access plus pydantic's ``model_dump``."""

    def model_dump(self, exclude_none: bool = False, mode: str = "python") -> dict[str, Any]:
        def dump(value: Any) -> Any:
            if isinstance(value, Item):
                return value.model_dump(exclude_none=exclude_none, mode=mode)
            if isinstance(value, list):
                return [dump(v) for v in value]
            return value

        return {k: dump(v) for k, v in vars(self).items() if not (exclude_none and v is None)}


def message_item(*parts: Any, id: str = "msg_1") -> Item:
    return Item(id=id, type="message", role="assistant", status="completed", content=list(parts))


def text_part(text: str) -> Item:
    return Item(type="output_text", text=text, annotations=[])


def refusal_part(text: str) -> Item:
    return Item(type="refusal", refusal=text)


def function_call_item(call_id: str, name: str, arguments: str, id: str = "fc_1") -> Item:
    return Item(id=id, type="function_call", call_id=call_id, name=name, arguments=arguments)


def reasoning_item(id: str = "rs_1") -> Item:
    return Item(id=id, type="reasoning", summary=[], encrypted_content="opaque")


def openai_response(
    *output: Any,
    status: str = "completed",
    incomplete_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="resp_1",
        model="gpt-5.6-terra-2026-06-01",
        status=status,
        incomplete_details=SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None,
        error=None,
        output=list(output),
        usage=SimpleNamespace(
            **{
                "input_tokens": 100,
                "output_tokens": 20,
                "input_tokens_details": SimpleNamespace(cached_tokens=0),
                "output_tokens_details": SimpleNamespace(reasoning_tokens=0),
                **(usage or {}),
            }
        ),
    )


def test_openai_request_translation() -> None:
    sdk = FakeOpenAISDK(openai_response(message_item(text_part("ok"))))
    client = OpenAIClient("gpt-5.6-terra", sdk_client=sdk)
    client.complete(CONVERSATION, tools=TOOLS, max_tokens=256)

    (request,) = sdk.requests
    assert request["model"] == "gpt-5.6-terra"
    assert request["instructions"] == "You fix bugs."
    assert request["max_output_tokens"] == 256
    assert request["store"] is False
    assert request["include"] == ["reasoning.encrypted_content"]
    assert "temperature" not in request  # reasoning models reject non-default values
    assert "reasoning" not in request  # effort left to the model unless configured
    assert request["tool_choice"] == "auto"
    assert request["tools"] == [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a line range",
            "parameters": TOOLS[0].parameters,
            "strict": False,
        }
    ]
    assert request["input"] == [
        {"role": "user", "content": "The cache expires early."},
        {"role": "assistant", "content": "Let me look."},
        {
            "type": "function_call",
            "call_id": "t1",
            "name": "read_file",
            "arguments": '{"path": "a.py"}',
        },
        {"type": "function_call_output", "call_id": "t1", "output": "def f(): ..."},
        {
            "type": "function_call",
            "call_id": "t2",
            "name": "read_file",
            "arguments": '{"path": "b.py"}',
        },
        {"type": "function_call_output", "call_id": "t2", "output": "no such file"},
    ]


def test_openai_replays_raw_items_instead_of_reconstructing_the_turn() -> None:
    raw_turn = (
        {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "opaque"},
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "t1",
            "name": "read_file",
            "arguments": "{}",
        },
    )
    messages = [
        user("go"),
        assistant("ignored text", (ToolCall("t1", "read_file", {}),), raw_items=raw_turn),
        tool_result("t1", "contents"),
    ]
    assert OpenAIClient.to_input(messages) == [
        {"role": "user", "content": "go"},
        *raw_turn,
        {"type": "function_call_output", "call_id": "t1", "output": "contents"},
    ]


def test_openai_sends_temperature_and_reasoning_effort_only_when_asked() -> None:
    sdk = FakeOpenAISDK(openai_response(message_item(text_part("ok"))))
    client = OpenAIClient("gpt-5.6-terra", sdk_client=sdk, reasoning_effort="low")
    client.complete([user("hi")], temperature=1.0)
    assert sdk.requests[0]["temperature"] == 1.0
    assert sdk.requests[0]["reasoning"] == {"effort": "low"}
    assert "tools" not in sdk.requests[0]
    assert "instructions" not in sdk.requests[0]


def test_openai_response_parsing_with_tool_calls_reasoning_and_cached_tokens() -> None:
    raw = openai_response(
        reasoning_item(),
        message_item(text_part("Reading "), text_part("now.")),
        function_call_item("call_a", "read_file", '{"path": "a.py"}', id="fc_a"),
        function_call_item("call_b", "read_file", '{"path": ', id="fc_b"),
        usage={
            "input_tokens": 400,
            "output_tokens": 90,
            "input_tokens_details": SimpleNamespace(cached_tokens=250),
            "output_tokens_details": SimpleNamespace(reasoning_tokens=60),
        },
    )
    client = OpenAIClient("gpt-5.6-terra", sdk_client=FakeOpenAISDK(raw))
    response = client.complete([user("hi")], tools=TOOLS)

    assert response.text == "Reading now."
    assert response.stop_reason is StopReason.TOOL_USE
    assert response.tool_calls[0] == ToolCall("call_a", "read_file", {"path": "a.py"})
    assert response.tool_calls[1].parse_error is not None
    assert response.usage == Usage(400, 90, cache_read_tokens=250, reasoning_tokens=60)
    assert response.cost_usd == pytest.approx(estimate_cost("gpt-5.6-terra", response.usage))
    assert response.model == "gpt-5.6-terra-2026-06-01"
    assert response.provider == "openai"
    assert response.response_id == "resp_1"
    # Every output item comes back as a plain dict, ready to be replayed next turn.
    assert [item["type"] for item in response.raw_items] == [
        "reasoning",
        "message",
        "function_call",
        "function_call",
    ]
    assert response.raw_items[0] == {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "opaque",
    }
    assert response.as_message().raw_items == response.raw_items


@pytest.mark.parametrize(
    "output, status, reason, expected",
    [
        ((message_item(text_part("x")),), "completed", None, StopReason.END_TURN),
        ((function_call_item("c", "read_file", "{}"),), "completed", None, StopReason.TOOL_USE),
        ((reasoning_item(),), "incomplete", "max_output_tokens", StopReason.MAX_TOKENS),
        ((message_item(text_part("x")),), "incomplete", "content_filter", StopReason.OTHER),
        ((message_item(refusal_part("no")),), "completed", None, StopReason.OTHER),
    ],
)
def test_openai_stop_reasons(output: Any, status: str, reason: Any, expected: StopReason) -> None:
    raw = openai_response(*output, status=status, incomplete_reason=reason)
    client = OpenAIClient("gpt-5.6-terra", sdk_client=FakeOpenAISDK(raw))
    response = client.complete([user("hi")])
    assert response.stop_reason is expected
    if expected is StopReason.MAX_TOKENS:
        assert response.text == "" and not response.tool_calls


def test_openai_failed_status_and_sdk_errors_become_model_errors() -> None:
    failed = openai_response(status="failed")
    failed.error = SimpleNamespace(message="server exploded")
    with pytest.raises(ModelError, match="response failed: server exploded"):
        OpenAIClient("gpt-5.6-terra", sdk_client=FakeOpenAISDK(failed)).complete([user("hi")])
    failing = FakeOpenAISDK(None, error=ConnectionError("boom"))
    with pytest.raises(ModelError, match="openai call to gpt-5.6-terra failed: ConnectionError"):
        OpenAIClient("gpt-5.6-terra", sdk_client=failing).complete([user("hi")])


# ----------------------------------------------------------------------------- factory


def test_client_for_picks_the_adapter_by_model_id() -> None:
    anthropic = client_for("claude-haiku-4-5-20251001", sdk_client=FakeAnthropicSDK(None))
    openai = client_for("gpt-5.6-luna", sdk_client=FakeOpenAISDK(None))
    assert isinstance(anthropic, AnthropicClient) and anthropic.model == "claude-haiku-4-5-20251001"
    assert isinstance(openai, OpenAIClient) and openai.model == "gpt-5.6-luna"


def test_tool_choice_none_keeps_tools_defined_but_unusable() -> None:
    """A decision point of the runtime: tool calls in the history, no tool use allowed."""
    sdk = FakeAnthropicSDK(anthropic_response(text_block("plan")))
    AnthropicClient("claude-sonnet-5", sdk_client=sdk).complete(
        CONVERSATION, tools=TOOLS, tool_choice="none"
    )
    assert sdk.requests[0]["tool_choice"] == {"type": "none"}
    assert [t["name"] for t in sdk.requests[0]["tools"]] == ["read_file"]

    sdk = FakeOpenAISDK(openai_response(message_item(text_part("plan"))))
    OpenAIClient("gpt-5.6-terra", sdk_client=sdk).complete(
        CONVERSATION, tools=TOOLS, tool_choice="none"
    )
    assert sdk.requests[0]["tool_choice"] == "none"
    assert [t["name"] for t in sdk.requests[0]["tools"]] == ["read_file"]

    # Without tools there is nothing to choose; the parameter is simply absent.
    sdk = FakeAnthropicSDK(anthropic_response(text_block("ok")))
    AnthropicClient("claude-sonnet-5", sdk_client=sdk).complete([user("hi")], tool_choice="none")
    assert "tool_choice" not in sdk.requests[0] and "tools" not in sdk.requests[0]
