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

    # A decision point of the structured runtime: tool calls in the history, a user
    # turn right after the tool result, tools defined but tool_choice="none" -- the
    # request must be accepted and answered in text.  The question must not need
    # the tool: asked for arithmetic with the tool disabled and a system prompt that
    # forbids computing, Haiku returned an empty reply and luna refused (issue #6).
    conversation += [answer.as_message(), user("Reply with the single word: done")]
    decided = llm.complete(conversation, tools=[ADD], max_tokens=2000, tool_choice="none")
    ledger.record(decided)
    assert decided.stop_reason is StopReason.END_TURN and not decided.tool_calls, decided
    assert "done" in decided.text.lower(), decided
    assert ledger.calls == 4 and ledger.cost_usd < 0.05


@pytest.mark.llm("anthropic")
def test_anthropic_round_trip() -> None:
    _round_trip(DEFAULT_CHEAP_MODEL)


@pytest.mark.llm("openai")
def test_openai_round_trip() -> None:
    _round_trip(OPENAI_CHEAP_MODEL)
