"""One real round trip per provider: text reply, then a tool call and its answer.

Paid and network-bound, so opt-in only: ``uv run pytest -m llm``.  Skipped
automatically when the provider's key is missing (see conftest) -- CI never
runs them.  Each test costs a fraction of a cent on the cheap models.
"""

from __future__ import annotations

import pytest

from repopilot.models import (
    Ledger,
    StopReason,
    ToolSpec,
    client_for,
    system,
    tool_result,
    user,
)
from repopilot.models.config import DEFAULT_CHEAP_MODEL, OPENAI_CHEAP_MODEL

ADD = ToolSpec(
    "add",
    "Add two integers and return the sum.",
    {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    },
)


def _round_trip(model: str) -> None:
    llm = client_for(model)
    ledger = Ledger(max_cost_usd=0.05)

    # Reasoning models spend output tokens thinking before they answer, so the
    # budgets are generous; the ledger cap still bounds the total spend.
    hello = llm.complete([system("Answer with one word."), user("Say hello.")], max_tokens=500)
    ledger.record(hello)
    assert hello.stop_reason is StopReason.END_TURN, hello
    assert hello.text.strip()
    assert hello.usage.input_tokens > 0 and hello.usage.output_tokens > 0
    assert hello.cost_usd > 0

    conversation = [
        system("Use the add tool for arithmetic; never compute it yourself."),
        user("What is 17 + 25? Reply with just the number once you know it."),
    ]
    asked = llm.complete(conversation, tools=[ADD], max_tokens=2000)
    ledger.record(asked)
    assert asked.stop_reason is StopReason.TOOL_USE, asked
    (call,) = asked.tool_calls
    assert call.name == "add" and call.parse_error is None
    assert {call.arguments["a"], call.arguments["b"]} == {17, 25}

    conversation += [asked.as_message(), tool_result(call.id, "42")]
    answer = llm.complete(conversation, tools=[ADD], max_tokens=2000)
    ledger.record(answer)
    assert answer.stop_reason is StopReason.END_TURN, answer
    assert "42" in answer.text
    assert ledger.calls == 3 and ledger.cost_usd < 0.05


@pytest.mark.llm("anthropic")
def test_anthropic_round_trip() -> None:
    _round_trip(DEFAULT_CHEAP_MODEL)


@pytest.mark.llm("openai")
def test_openai_round_trip() -> None:
    _round_trip(OPENAI_CHEAP_MODEL)
