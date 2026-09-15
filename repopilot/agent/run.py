"""What every agent returns: a termination reason and the run's record.

Shared by the baseline loop (Phase 1) and the structured runtime (Phase 2) so
the harness, the metrics and the failure taxonomy treat both the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from repopilot.tracing.events import Trace, clip


class Termination(StrEnum):
    DONE = "done"  # baseline: the model ended its turn; runtime: verified green
    BUDGET_STEPS = "budget_steps"
    BUDGET_TOOL_CALLS = "budget_tool_calls"
    BUDGET_TEST_RUNS = "budget_test_runs"  # runtime: a changed patch could not be verified
    BUDGET_TOKENS = "budget_tokens"
    BUDGET_COST = "budget_cost"
    BUDGET_RUNTIME = "budget_runtime"
    MODEL_ERROR = "model_error"  # provider failure after the SDK's retries
    MODEL_TRUNCATED = "model_truncated"  # reply cut off by max output tokens, no tool calls
    MODEL_STOPPED = "model_stopped"  # refusal / filter / other provider stop
    AGENT_LOOP = "agent_loop"  # runtime: repeated the same call after a replanning notice
    NO_PROGRESS = "no_progress"  # runtime: two turns without a new edit when one was due

    @property
    def is_budget(self) -> bool:
        return self.value.startswith("budget_")


BUDGET_TERMINATION = {
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
    runtime: dict[str, Any] | None = None  # structured-runtime extras (phases, interventions)

    def to_record(self) -> dict[str, Any]:
        """JSON-serializable summary stored next to the verdict (results.jsonl)."""
        record = {
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
        if self.runtime is not None:
            record["runtime"] = self.runtime
        return record
