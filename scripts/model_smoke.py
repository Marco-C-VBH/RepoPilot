#!/usr/bin/env python
"""Smoke-test the model layer against the real providers.

Usage:  uv run python scripts/model_smoke.py [--models claude-sonnet-5 gpt-5.6-luna ...]

For every model (default: the configured strong and cheap models) it makes one
tiny text call and one tool-call round trip, then prints latency, tokens and
the estimated cost.  Total cost is a few tenths of a cent.  Exit code 1 if any
model fails -- the usual causes are a missing key (.env), a wrong model id, or
a price missing from repopilot/models/pricing.py.
"""

from __future__ import annotations

import argparse
import sys

from repopilot.models import (
    ConfigError,
    Ledger,
    ModelError,
    ModelSettings,
    StopReason,
    ToolSpec,
    client_for,
    load_env,
    system,
    tool_result,
    user,
)

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


def smoke(model: str) -> bool:
    print(f"== {model}")
    ledger = Ledger(max_cost_usd=0.10)
    try:
        llm = client_for(model)
        print(f"   provider: {llm.provider}")
        hello = llm.complete([system("Answer with one word."), user("Say hello.")], max_tokens=500)
        ledger.record(hello)
        tokens = hello.usage.total_tokens
        print(f"   text: {hello.text.strip()!r}  {hello.latency_ms} ms  {tokens} tok")

        conversation = [
            system("Use the add tool for arithmetic; never compute it yourself."),
            user("What is 17 + 25? Reply with just the number once you know it."),
        ]
        asked = llm.complete(conversation, tools=[ADD], max_tokens=2000)
        ledger.record(asked)
        if asked.stop_reason is not StopReason.TOOL_USE or not asked.tool_calls:
            print(f"   FAIL: expected a tool call, got {asked.stop_reason} {asked.text!r}")
            return False
        call = asked.tool_calls[0]
        print(f"   tool: {call.name}({call.arguments})  {asked.latency_ms} ms")
        if call.parse_error:
            print(f"   FAIL: {call.parse_error}")
            return False

        conversation += [asked.as_message(), tool_result(call.id, "42")]
        answer = llm.complete(conversation, tools=[ADD], max_tokens=2000)
        ledger.record(answer)
        print(f"   answer: {answer.text.strip()!r}  served by {answer.model}")
        if answer.usage.reasoning_tokens:
            print(f"   reasoning tokens in the last call: {answer.usage.reasoning_tokens}")
        ok = "42" in answer.text
    except (ConfigError, ModelError) as exc:
        print(f"   FAIL: {exc}")
        return False
    totals = ledger.to_record()
    print(
        f"   {ledger.calls} calls, {totals['input_tokens']} in / {totals['output_tokens']} out, "
        f"${ledger.cost_usd:.5f}, {ledger.latency_ms} ms total -> {'OK' if ok else 'FAIL'}"
    )
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--models", nargs="+", help="model ids to try (default: strong + cheap)")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args(argv)

    load_env(args.env_file)
    try:
        if args.models:
            models = list(dict.fromkeys(args.models))
        else:
            settings = ModelSettings.from_env()
            models = list(dict.fromkeys([settings.strong, settings.cheap]))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    results = [smoke(m) for m in models]
    print(f"\n{sum(results)}/{len(results)} model(s) OK")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
