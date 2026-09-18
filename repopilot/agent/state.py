"""The structured runtime's state (spec §5.2) and its phases (spec §5.3).

``AgentState`` is what the runtime knows between model calls, independent of the
conversation history: the plan, the hypotheses and their fate, which files were
visited, every test run with what it fixed and what it broke, and the counters
behind the run's metrics.  It is written to the trace at every phase transition
so a run can be audited without replaying the messages -- and it is the seed of
the compacted context Phase 2b will send instead of the full history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from repopilot.tracing.events import clip


class Phase(StrEnum):
    INITIALIZE = "initialize"  # runtime: workspace + reproduction run
    PLAN = "plan"  # model, no tools: plan and suspects as JSON
    LOCALIZE = "localize"  # model: search and read; ends with a hypothesis
    PATCH = "patch"  # model: edit; ends when the turn ends with a changed diff
    TEST = "test"  # runtime: the task's full test command
    ANALYZE = "analyze"  # model, no tools: what the failure means, where to go next
    FINALIZE = "finalize"  # model, no tools: only when the suite was green from the start
    DONE = "done"


@dataclass
class Hypothesis:
    text: str
    step: int
    status: str = "active"  # active | tested | rejected

    def to_record(self) -> dict[str, Any]:
        return {"step": self.step, "status": self.status, "text": clip(self.text, 600)}


@dataclass
class TestSummary:
    """One runtime-owned test run, judged against the reproduction run."""

    step: int
    phase: str  # "reproduce" or "verify"
    passed: int
    failed: int
    errors: int
    fixed: list[str]
    still_failing: list[str]
    newly_failing: list[str]
    is_error: bool  # no report / timeout / patch failed to apply
    detail: str

    @property
    def green(self) -> bool:
        return not self.is_error and self.failed == 0 and self.errors == 0

    def to_record(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "phase": self.phase,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "fixed": len(self.fixed),
            "still_failing": len(self.still_failing),
            "newly_failing": self.newly_failing[:20],
            "green": self.green,
            "is_error": self.is_error,
        }


@dataclass
class AgentState:
    task_id: str
    phase: Phase = Phase.INITIALIZE
    plan: list[str] = field(default_factory=list)
    suspects: list[str] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    diagnoses: list[str] = field(default_factory=list)  # ANALYZE replies, in order
    notes: list[str] = field(default_factory=list)  # runtime events, dated by step
    visited_files: list[str] = field(default_factory=list)
    reads: list[tuple[str, int, int]] = field(default_factory=list)  # (path, start, end)
    searches: list[str] = field(default_factory=list)  # queries / names looked up
    initial_failures: list[str] = field(default_factory=list)
    test_history: list[TestSummary] = field(default_factory=list)
    patches_tested: list[str] = field(default_factory=list)  # diffs at each verify run
    transitions: list[dict[str, Any]] = field(default_factory=list)
    steps_by_phase: dict[str, int] = field(default_factory=dict)
    # interventions, all counted for the metrics
    loop_interventions: int = 0
    repeated_tool_calls: int = 0  # identical calls, same workspace version
    rereads: int = 0  # ... of which the earlier result had left a compact window
    refused_tool_calls: int = 0
    forced_transitions: int = 0
    nudges: int = 0
    analyze_rounds: int = 0
    workspace_resets: int = 0
    finalize_continues: int = 0
    first_green_step: int | None = None

    @property
    def suite_was_green(self) -> bool:
        """True when the reproduction run found nothing failing (hidden-only tasks)."""
        first = self.test_history[0] if self.test_history else None
        return first is not None and first.green

    @property
    def active_hypothesis(self) -> Hypothesis | None:
        for h in reversed(self.hypotheses):
            if h.status == "active":
                return h
        return None

    def enter(self, phase: Phase, step: int, reason: str | None = None) -> None:
        self.transitions.append(
            {"from": str(self.phase), "to": str(phase), "step": step, "reason": reason}
        )
        self.phase = phase

    def count_step(self) -> None:
        key = str(self.phase)
        self.steps_by_phase[key] = self.steps_by_phase.get(key, 0) + 1

    def visit(self, path: str) -> None:
        if path and path not in self.visited_files:
            self.visited_files.append(path)

    def to_record(self, *, full: bool = False) -> dict[str, Any]:
        """The trace snapshot (``full``) or the compact summary stored with the result."""
        record: dict[str, Any] = {
            "phase": str(self.phase),
            "plan": self.plan,
            "suspects": self.suspects,
            "hypotheses": [h.to_record() for h in self.hypotheses],
            "diagnoses": [clip(d, 400) for d in self.diagnoses],
            "notes": [clip(n, 300) for n in self.notes],
            "initial_failures": len(self.initial_failures),
            "test_history": [t.to_record() for t in self.test_history],
            "steps_by_phase": dict(self.steps_by_phase),
            "loop_interventions": self.loop_interventions,
            "repeated_tool_calls": self.repeated_tool_calls,
            "rereads": self.rereads,
            "refused_tool_calls": self.refused_tool_calls,
            "forced_transitions": self.forced_transitions,
            "nudges": self.nudges,
            "analyze_rounds": self.analyze_rounds,
            "workspace_resets": self.workspace_resets,
            "finalize_continues": self.finalize_continues,
            "first_green_step": self.first_green_step,
        }
        if full:
            record["visited_files"] = list(self.visited_files)
            record["reads"] = [list(r) for r in self.reads]
            record["searches"] = list(self.searches)
            record["initial_failing_tests"] = self.initial_failures[:50]
            record["transitions"] = list(self.transitions)
        return record
