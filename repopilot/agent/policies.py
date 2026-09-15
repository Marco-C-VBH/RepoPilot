"""Runtime policies (spec §5.4, §9.2): what the model may do in each phase, and
when the runtime steps in.

Everything here is a pure decision -- no I/O -- so it can be unit-tested
without a model, and so the runtime loop stays readable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from repopilot.agent.state import Phase

SEARCH_TOOLS = ("search_code", "search_symbol", "find_references", "read_file")
PHASE_TOOLS: dict[Phase, tuple[str, ...]] = {
    Phase.PLAN: (),
    Phase.LOCALIZE: SEARCH_TOOLS,
    Phase.PATCH: (*SEARCH_TOOLS, "edit_file"),
    Phase.ANALYZE: (),
    Phase.FINALIZE: (),
}


@dataclass(frozen=True)
class RuntimeLimits:
    """Per-phase limits, on top of the run-level ``AgentBudget``."""

    localize_tool_calls: int = 10  # per LOCALIZE visit, then the hypothesis is requested
    patch_edits: int = 4  # per PATCH visit, then edits are refused until the tests run
    # The n-th identical call whose earlier results the model can still see is
    # refused with a replanning notice; repeating it while the notice is still in
    # view ends the run as agent_loop.
    loop_notice_at: int = 3
    finalize_continues: int = 1  # how often FINALIZE may send the model back

    def to_record(self) -> dict[str, Any]:
        return {
            "localize_tool_calls": self.localize_tool_calls,
            "patch_edits": self.patch_edits,
            "loop_notice_at": self.loop_notice_at,
            "finalize_continues": self.finalize_continues,
        }


DEFAULT_LIMITS = RuntimeLimits()


def call_signature(name: str, arguments: dict[str, Any] | None) -> str:
    """Tool name plus canonical JSON of the arguments: identical calls, identical key."""
    return name + " " + json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"))


@dataclass
class LoopDetector:
    """Counts identical tool calls (spec §9.2).

    Two calls are identical when the tool, the canonical arguments and the
    workspace ``version`` match: after an edit or a reset the same read or
    search is a new question with a possibly new answer.  ``check`` returns
    ``None`` (fine), ``"notice"`` (refuse this call and ask the model to change
    approach) or ``"terminate"`` (the model repeated the call while the notice
    was still in front of it).

    Only what the model can still see counts: occurrences at steps ``>= since``.
    The full-history context passes ``since=0``; the compact context passes the
    start of its window, so re-reading something whose result has left the
    context is not a loop (issue #8) -- the notice tells the model the result
    is above, and it must be.  Such re-reads are tallied in ``rereads`` as the
    price of compaction.
    """

    limits: RuntimeLimits = DEFAULT_LIMITS
    seen: dict[str, list[int]] = field(default_factory=dict)  # signature -> steps
    noticed: dict[str, int] = field(default_factory=dict)  # signature -> step of the notice
    rereads: int = 0  # identical calls whose earlier result had left the window

    def check(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        version: int = 0,
        step: int = 0,
        since: int = 0,
    ) -> str | None:
        key = f"{call_signature(name, arguments)} @v{version}"
        steps = self.seen.setdefault(key, [])
        steps.append(step)
        visible = sum(1 for s in steps if s >= since)
        if visible == 1 and len(steps) > 1:
            self.rereads += 1
        notice = self.noticed.get(key)
        if notice is not None and notice >= since:
            return "terminate"
        if visible >= self.limits.loop_notice_at:
            self.noticed[key] = step
            return "notice"
        return None

    def repeats(self) -> int:
        """How many calls repeated an earlier identical call (same workspace version)."""
        return sum(len(steps) - 1 for steps in self.seen.values() if len(steps) > 1)


def phase_refusal(phase: Phase, name: str) -> str:
    """Why a tool outside the phase's set is refused, and what to do instead."""
    allowed = ", ".join(PHASE_TOOLS.get(phase, ())) or "none"
    if phase is Phase.LOCALIZE and name == "edit_file":
        return (
            "edit_file is not available while localizing. Reply without tool calls to state "
            "your hypothesis (file, function, cause, intended change); you can edit right after."
        )
    if name == "run_tests":
        return (
            "run_tests is not available: the runtime runs the full test command itself as soon "
            "as you end a patching turn with an edit in place."
        )
    return f"{name} is not available in the {phase} phase (available: {allowed})."


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_reply(text: str) -> dict[str, Any] | None:
    """The first JSON object in a reply (fenced or bare), or None."""
    if not text:
        return None
    candidates = [text.strip()]
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates = fenced + candidates
    for candidate in candidates:
        match = _JSON_BLOCK.search(candidate)
        if not match:
            continue
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def string_list(value: Any, limit: int = 8) -> list[str]:
    """Coerce a JSON field into a short list of strings."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:limit]
