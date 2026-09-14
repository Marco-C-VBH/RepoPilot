"""Per-run limits (spec §9.1) and the counters that enforce them.

Every benchmark configuration runs under the same ``AgentBudget`` (or an
explicitly documented one), so success rates stay comparable.  The tracker
never raises: the runtime asks ``exceeded()`` before each step and after each
model call and terminates with the name of the limit that was crossed, which
becomes the run's termination reason and, downstream, a failure category.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from repopilot.models.ledger import Ledger


@dataclass(frozen=True)
class AgentBudget:
    max_steps: int = 30  # model calls
    max_tool_calls: int = 40  # tool invocations the model asks for (valid or not)
    max_test_runs: int = 5  # run_tests invocations that actually execute
    max_tokens: int = 100_000  # input + output tokens summed over the run
    max_cost_usd: float = 0.50
    max_runtime_seconds: float = 600.0

    def to_record(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_test_runs": self.max_test_runs,
            "max_tokens": self.max_tokens,
            "max_cost_usd": self.max_cost_usd,
            "max_runtime_seconds": self.max_runtime_seconds,
        }

    def replace(self, **changes: Any) -> AgentBudget:
        data = self.to_record()
        unknown = set(changes) - set(data)
        if unknown:
            raise ValueError(f"unknown budget field(s): {sorted(unknown)}")
        data.update({k: v for k, v in changes.items() if v is not None})
        return AgentBudget(**data)


DEFAULT_BUDGET = AgentBudget()


class BudgetTracker:
    """Counts what a run has used and names the first limit it crosses."""

    def __init__(
        self,
        budget: AgentBudget,
        ledger: Ledger | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budget = budget
        self.ledger = ledger if ledger is not None else Ledger()
        self._clock = clock
        self._started = clock()
        self.steps = 0
        self.tool_calls = 0
        self.test_runs = 0

    @property
    def elapsed_seconds(self) -> float:
        return self._clock() - self._started

    def can_run_tests(self) -> bool:
        return self.test_runs < self.budget.max_test_runs

    def can_call_tool(self) -> bool:
        return self.tool_calls < self.budget.max_tool_calls

    def exceeded(self) -> str | None:
        """The limit that stops the run now, or None.  Checked before every model
        call (steps / runtime) and after every call (tokens / cost)."""
        b = self.budget
        if self.ledger.cost_usd > b.max_cost_usd:
            return "cost"
        if self.ledger.usage.total_tokens > b.max_tokens:
            return "tokens"
        if self.elapsed_seconds > b.max_runtime_seconds:
            return "runtime"
        if self.steps >= b.max_steps:
            return "steps"
        if self.tool_calls >= b.max_tool_calls:
            return "tool_calls"
        return None

    def to_record(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "test_runs": self.test_runs,
            "tokens": self.ledger.usage.total_tokens,
            "cost_usd": round(self.ledger.cost_usd, 6),
            "runtime_seconds": round(self.elapsed_seconds, 3),
            "limits": self.budget.to_record(),
        }
