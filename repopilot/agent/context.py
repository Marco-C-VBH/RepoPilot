"""Context strategies for the structured runtime (spec §8 / §12.3).

``full``     every model call replays the whole conversation (Phase 2a; the
             control arm of the context ablation).
``compact``  every model call is rebuilt from the runtime's working state plus
             the last few tool steps verbatim (Phase 2b):

    system     the runtime prompt + how to read the working state
    user       WORKING STATE: task, initial failures, plan, hypotheses, files
               read (ranges only), searches made, current patch, latest test
               run, budget left  -- rendered from ``AgentState``, no model summary
    ...        the last ``window_steps`` tool steps, whole (assistant message
               with its tool calls, the tool results, any runtime nudge)
    user       the current phase's instructions

Nothing here is produced by a model: the state is rendered deterministically
from what the runtime already tracks, so the arm costs no extra calls and the
same run always yields the same prompt shapes.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from repopilot.agent.budget import BudgetTracker
from repopilot.agent.prompts import TaskInput
from repopilot.agent.state import AgentState, TestSummary
from repopilot.models.types import Message, system, user
from repopilot.tracing.events import clip

CONTEXT_MODES = ("full", "compact")

COMPACT_NOTE = """\

How your context works: every turn starts with a WORKING STATE the runtime keeps \
current (task, plan, hypotheses, files you have read, your current patch, the latest \
test run, budget left), followed by your most recent tool calls and their results \
verbatim. Older tool outputs are not repeated; if you need code you read earlier, \
read it again. Your hypothesis and diagnoses are kept in the state.
"""


@dataclass(frozen=True)
class ContextConfig:
    mode: str = "full"
    window_steps: int = 3  # tool steps kept verbatim in compact mode
    patch_chars: int = 2000  # the current diff, clipped, in the state
    failure_chars: int = 200  # per failing test, in the state
    max_failures: int = 8

    def __post_init__(self) -> None:
        if self.mode not in CONTEXT_MODES:
            raise ValueError(f"context mode must be one of {CONTEXT_MODES}, not {self.mode!r}")

    @property
    def compact(self) -> bool:
        return self.mode == "compact"

    def to_record(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "window_steps": self.window_steps,
            "patch_chars": self.patch_chars,
            "failure_chars": self.failure_chars,
            "max_failures": self.max_failures,
        }


DEFAULT_CONTEXT = ContextConfig()


# ------------------------------------------------------------------- working state

_FAILURE_LINE = re.compile(r"^(FAILED|ERROR) (\S+)$")


def failure_lines(detail: str, *, limit: int, chars: int) -> list[str]:
    """``nodeid — first line of the failure`` for each failure in a run summary."""
    lines = detail.splitlines()
    out: list[str] = []
    for index, line in enumerate(lines):
        match = _FAILURE_LINE.match(line.strip())
        if not match:
            continue
        message = ""
        if index + 1 < len(lines) and lines[index + 1].startswith("  "):
            message = lines[index + 1].strip()
        out.append(f"- {match.group(2)}" + (f" — {clip(message, chars)}" if message else ""))
        if len(out) >= limit:
            break
    return out


def _test_section(summary: TestSummary | None, config: ContextConfig, *, initial: int) -> str:
    if summary is None:
        return "Latest test run with your change: not run yet."
    if summary.is_error:
        return "Latest test run with your change: could not complete —\n" + clip(
            summary.detail, 600
        )
    head = (
        f"Latest test run with your change: {summary.passed} passed, {summary.failed} failed, "
        f"{summary.errors} errors; fixed {len(summary.fixed)} of {initial} initially failing, "
        f"{len(summary.still_failing)} still failing, {len(summary.newly_failing)} newly failing."
    )
    details = failure_lines(summary.detail, limit=config.max_failures, chars=config.failure_chars)
    return head + ("\n" + "\n".join(details) if details else "")


def render_state(
    task: TaskInput,
    state: AgentState,
    tracker: BudgetTracker,
    config: ContextConfig,
    *,
    diff: str,
) -> str:
    """The WORKING STATE message, rendered from what the runtime tracks."""
    name = task.repo.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    parts = [
        f"WORKING STATE (step {tracker.steps}, phase {state.phase}) — kept current by the "
        "runtime; older tool outputs are not replayed.",
        f"Repository: {name} ({task.repo}) at commit {task.base_commit[:12]}\n"
        f"Test command: {task.test_command}\n\nBug report:\n{task.description.strip()}",
    ]

    first = state.test_history[0] if state.test_history else None
    if first is None:
        parts.append("Initial test run: not run.")
    elif first.is_error:
        parts.append("Initial test run: could not complete —\n" + clip(first.detail, 600))
    elif first.green:
        parts.append(
            f"Initial test run: all {first.passed} tests passed before any change, so the "
            "visible suite will not point at the bug; rely on the report. Hidden regression "
            "tests decide the task."
        )
    else:
        lines = failure_lines(first.detail, limit=config.max_failures, chars=config.failure_chars)
        more = len(state.initial_failures) - len(lines)
        parts.append(
            f"Initial test run (before any change): {first.failed} failed, {first.passed} passed. "
            "Failing:\n"
            + "\n".join(lines)
            + (f"\n- ... {more} more" if more > 0 else "")
            + "\nFailing tests point at the behaviour to fix; hidden regression tests also decide."
        )

    if state.plan:
        parts.append("Plan:\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(state.plan, 1)))
    if state.suspects:
        parts.append("Suspects: " + ", ".join(state.suspects))
    if state.hypotheses:
        parts.append(
            "Hypotheses:\n"
            + "\n".join(f"- [{h.status}] {clip(h.text, 600)}" for h in state.hypotheses)
        )
    if state.diagnoses:
        parts.append(
            "Your diagnoses after failed test runs:\n"
            + "\n".join(f"- {clip(d, 400)}" for d in state.diagnoses)
        )

    if state.reads:
        by_path: dict[str, list[str]] = {}
        for path, start, end in state.reads:
            by_path.setdefault(path, []).append(f"{start}-{end}")
        listed = "; ".join(f"{path} {', '.join(ranges)}" for path, ranges in by_path.items())
        parts.append(
            "Files read (line ranges; contents not shown — read again if you need them): " + listed
        )
    if state.searches:
        parts.append("Searches made: " + ", ".join(repr(q) for q in state.searches[-12:]))

    if diff.strip():
        shown = (
            diff
            if len(diff) <= config.patch_chars
            else (
                diff[: config.patch_chars].rstrip()
                + f"\n... [diff truncated at {config.patch_chars} chars]"
            )
        )
        parts.append("Current patch (diff against the original tree):\n" + shown)
    else:
        parts.append("Current patch: none yet.")

    verify = next((t for t in reversed(state.test_history) if t.phase == "verify"), None)
    parts.append(_test_section(verify, config, initial=len(state.initial_failures)))

    b = tracker.budget
    parts.append(
        f"Budget left: {b.max_steps - tracker.steps} of {b.max_steps} steps, "
        f"{b.max_tool_calls - tracker.tool_calls} of {b.max_tool_calls} tool calls, "
        f"{b.max_test_runs - tracker.test_runs} of {b.max_test_runs} test runs, "
        f"~{max(b.max_tokens - tracker.ledger.usage.total_tokens, 0) // 1000}k of "
        f"{b.max_tokens // 1000}k tokens."
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------- assembly

WINDOW_KINDS = ("assistant", "tool", "nudge")


def compact_messages(
    *,
    system_prompt: str,
    state_text: str,
    instruction: str,
    history: Sequence[Message],
    meta: Sequence[tuple[int, str]],
    current_step: int,
    window_steps: int,
) -> list[Message]:
    """System + working state + the last ``window_steps`` tool steps + the instruction.

    ``meta`` gives, for each message of the full history, the step it belongs
    to and its kind; steps ``>= current_step - window_steps`` are kept, whole,
    so tool calls and their results always travel together.
    """
    cutoff = current_step - window_steps
    window = [
        message
        for message, (step, kind) in zip(history, meta, strict=True)
        if kind in WINDOW_KINDS
        and step >= cutoff
        # A text-only reply is a decision (plan, hypothesis, analysis, summary)
        # and lives in the working state; only tool-calling turns are replayed.
        and (kind != "assistant" or message.tool_calls)
    ]
    messages: list[Message] = [system(system_prompt + COMPACT_NOTE)]
    if not window:
        messages.append(user(state_text + "\n\n" + instruction))
        return messages
    messages.append(user(state_text))
    messages.extend(window)
    messages.append(user(instruction))
    return messages
