"""The Phase 1 baseline: a plain tool-calling loop under a budget.

    messages = [system prompt, bug report]
    loop:
        model(messages, tools) -> text and/or tool calls
        no tool calls          -> done
        else run each call through the Toolbox, append the results, continue
    stop early when the budget (steps, tool calls, tokens, cost, runtime) is spent

This is the *unconstrained* end of the architecture ablation (spec §12.2): no
state machine, no planning phase, no loop detection, no context compaction.
Everything it does is recorded in a ``Trace``; the workspace diff at the end is
the candidate patch.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from repopilot.agent.budget import DEFAULT_BUDGET, AgentBudget, BudgetTracker
from repopilot.agent.prompts import SYSTEM_PROMPT, TaskInput, task_prompt
from repopilot.models.client import ModelClient, ModelError
from repopilot.models.ledger import Ledger
from repopilot.models.types import Message, StopReason, ToolCall, system, tool_result, user
from repopilot.tools.results import ToolResult
from repopilot.tools.toolbox import Toolbox
from repopilot.tracing.events import Trace, clip, clip_arguments

DEFAULT_MAX_OUTPUT_TOKENS = 6000


class Termination(StrEnum):
    DONE = "done"  # the model ended its turn without tool calls
    BUDGET_STEPS = "budget_steps"
    BUDGET_TOOL_CALLS = "budget_tool_calls"
    BUDGET_TOKENS = "budget_tokens"
    BUDGET_COST = "budget_cost"
    BUDGET_RUNTIME = "budget_runtime"
    MODEL_ERROR = "model_error"  # provider failure after the SDK's retries
    MODEL_TRUNCATED = "model_truncated"  # reply cut off by max output tokens, no tool calls
    MODEL_STOPPED = "model_stopped"  # refusal / filter / other provider stop

    @property
    def is_budget(self) -> bool:
        return self.value.startswith("budget_")


_BUDGET_TERMINATION = {
    "steps": Termination.BUDGET_STEPS,
    "tool_calls": Termination.BUDGET_TOOL_CALLS,
    "tokens": Termination.BUDGET_TOKENS,
    "cost": Termination.BUDGET_COST,
    "runtime": Termination.BUDGET_RUNTIME,
}


@dataclass
class AgentRun:
    """Everything the harness and the metrics need to know about one run."""

    termination: Termination
    detail: str | None
    steps: int
    tool_calls: int
    invalid_tool_calls: int
    test_runs: int
    test_runs_refused: int
    elapsed_seconds: float
    ledger: dict[str, Any]
    final_text: str
    patch: str
    changed_files: list[str]
    files_read: list[str]
    files_edited: list[str]
    trace: Trace = field(repr=False)

    def to_record(self) -> dict[str, Any]:
        """JSON-serializable summary stored next to the verdict (results.jsonl)."""
        return {
            "termination": str(self.termination),
            "detail": self.detail,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "invalid_tool_calls": self.invalid_tool_calls,
            "test_runs": self.test_runs,
            "test_runs_refused": self.test_runs_refused,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "input_tokens": self.ledger["input_tokens"],
            "output_tokens": self.ledger["output_tokens"],
            "cache_read_tokens": self.ledger["cache_read_tokens"],
            "reasoning_tokens": self.ledger["reasoning_tokens"],
            "cost_usd": self.ledger["cost_usd"],
            "model_latency_ms": self.ledger["latency_ms"],
            "by_model": self.ledger["by_model"],
            "patch_bytes": len(self.patch.encode("utf-8")),
            "changed_files": self.changed_files,
            "files_read": self.files_read,
            "files_edited": self.files_edited,
            "final_text": clip(self.final_text, 1000),
        }


class BaselineAgent:
    def __init__(
        self,
        client: ModelClient,
        toolbox: Toolbox,
        budget: AgentBudget = DEFAULT_BUDGET,
        *,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self.client = client
        self.toolbox = toolbox
        self.budget = budget
        self.max_output_tokens = max_output_tokens
        self.system_prompt = system_prompt

    def run(self, task: TaskInput) -> AgentRun:
        trace = Trace()
        tracker = BudgetTracker(self.budget, Ledger())
        messages: list[Message] = [system(self.system_prompt), user(task_prompt(task))]
        invalid_tool_calls = 0
        test_runs_refused = 0
        files_read: list[str] = []
        files_edited: list[str] = []
        final_text = ""

        trace.add(
            "run_start",
            task_id=task.task_id,
            model=self.client.model,
            provider=self.client.provider,
            budget=self.budget.to_record(),
            tools=[spec.name for spec in self.toolbox.specs()],
        )

        def finish(termination: Termination, detail: str | None = None) -> AgentRun:
            patch = self.toolbox.workspace.diff()
            changed = self.toolbox.workspace.changed_files()
            trace.add("patch", changed_files=changed, bytes=len(patch.encode("utf-8")), diff=patch)
            trace.add(
                "run_end",
                termination=str(termination),
                detail=detail,
                budget=tracker.to_record(),
                invalid_tool_calls=invalid_tool_calls,
                test_runs_refused=test_runs_refused,
                ledger=tracker.ledger.to_record(),
                final_text=clip(final_text),
            )
            return AgentRun(
                termination=termination,
                detail=detail,
                steps=tracker.steps,
                tool_calls=tracker.tool_calls,
                invalid_tool_calls=invalid_tool_calls,
                test_runs=tracker.test_runs,
                test_runs_refused=test_runs_refused,
                elapsed_seconds=tracker.elapsed_seconds,
                ledger=tracker.ledger.to_record(),
                final_text=final_text,
                patch=patch,
                changed_files=changed,
                files_read=sorted(set(files_read)),
                files_edited=sorted(set(files_edited)),
                trace=trace,
            )

        while True:
            limit = tracker.exceeded()
            if limit:
                return finish(_BUDGET_TERMINATION[limit], f"{limit} limit reached before step")

            tracker.steps += 1
            try:
                response = self.client.complete(
                    messages, tools=self.toolbox.specs(), max_tokens=self.max_output_tokens
                )
            except ModelError as exc:
                trace.add("model_call", step=tracker.steps, error=str(exc))
                return finish(Termination.MODEL_ERROR, str(exc))
            tracker.ledger.record(response)
            trace.add(
                "model_call",
                step=tracker.steps,
                **response.to_record(),
                text=clip(response.text),
                calls=[
                    {"id": c.id, "name": c.name, "arguments": clip_arguments(c.arguments)}
                    for c in response.tool_calls
                ],
            )
            messages.append(response.as_message())
            if response.text:
                final_text = response.text

            if not response.tool_calls:
                if response.stop_reason is StopReason.MAX_TOKENS:
                    return finish(Termination.MODEL_TRUNCATED, "reply cut off by max_tokens")
                if response.stop_reason is StopReason.OTHER:
                    return finish(Termination.MODEL_STOPPED, "provider stopped the reply")
                return finish(Termination.DONE)

            for index, call in enumerate(response.tool_calls):
                started = time.perf_counter()
                if not tracker.can_call_tool():
                    result = ToolResult.error(
                        f"tool-call budget exhausted ({self.budget.max_tool_calls}); "
                        "stop and summarize what you found"
                    )
                elif call.name == "run_tests" and not tracker.can_run_tests():
                    test_runs_refused += 1
                    result = ToolResult.error(
                        f"test-run budget exhausted ({self.budget.max_test_runs} of "
                        f"{self.budget.max_test_runs} used); finish with your current edits"
                    )
                else:
                    tracker.tool_calls += 1
                    result = self._execute(call)
                    if call.name == "run_tests" and not result.meta.get("invalid"):
                        tracker.test_runs += 1
                if result.meta.get("invalid") or call.parse_error:
                    invalid_tool_calls += 1
                if not result.is_error:
                    if call.name == "read_file":
                        files_read.append(call.arguments.get("path", ""))
                    elif call.name == "edit_file":
                        files_edited.append(call.arguments.get("path", ""))
                trace.add(
                    "tool_call",
                    step=tracker.steps,
                    index=index,
                    id=call.id,
                    name=call.name,
                    arguments=clip_arguments(call.arguments),
                    parse_error=call.parse_error,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    is_error=result.is_error,
                    invalid=bool(result.meta.get("invalid") or call.parse_error),
                    meta={k: v for k, v in result.meta.items() if k != "invalid"},
                    output=clip(result.output),
                )
                messages.append(tool_result(call.id, result.output, is_error=result.is_error))

            limit = tracker.exceeded()
            if limit in ("tokens", "cost"):
                return finish(_BUDGET_TERMINATION[limit], f"{limit} limit reached")

    def _execute(self, call: ToolCall) -> ToolResult:
        if call.parse_error:
            return ToolResult.error(
                f"{call.name}: {call.parse_error}; resend the call with valid JSON arguments",
                invalid=True,
            )
        return self.toolbox.call(call.name, call.arguments)
