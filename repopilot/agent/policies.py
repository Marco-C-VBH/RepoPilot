"""Runtime policies (spec §5.4, §9.2): what the model may do in each phase, and
when the runtime steps in.

Everything here is a pure decision -- no I/O -- so it can be unit-tested
without a model, and so the runtime loop stays readable.
"""

from __future__ import annotations

import json
import re
from collections import Counter
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
    loop_notice_at: int = 3  # the n-th identical call is refused with a replanning notice
    loop_terminate_at: int = 4  # the n-th identical call ends the run as agent_loop
    finalize_continues: int = 1  # how often FINALIZE may send the model back

    def to_record(self) -> dict[str, Any]:
        return {
            "localize_tool_calls": self.localize_tool_calls,
            "patch_edits": self.patch_edits,
            "loop_notice_at": self.loop_notice_at,
            "loop_terminate_at": self.loop_terminate_at,
            "finalize_continues": self.finalize_continues,
        }


DEFAULT_LIMITS = RuntimeLimits()


def call_signature(name: str, arguments: dict[str, Any] | None) -> str:
    """Tool name plus canonical JSON of the arguments: identical calls, identical key."""
    return name + " " + json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"))


@dataclass
class LoopDetector:
    """Counts identical tool calls (spec §9.2).

    ``check`` returns ``None`` (fine), ``"notice"`` (refuse this call and ask
    the model to change approach) or ``"terminate"`` (the model repeated the
    call after the notice).
    """

    limits: RuntimeLimits = DEFAULT_LIMITS
    seen: Counter[str] = field(default_factory=Counter)

    def check(self, name: str, arguments: dict[str, Any] | None) -> str | None:
        key = call_signature(name, arguments)
        self.seen[key] += 1
        count = self.seen[key]
        if count >= self.limits.loop_terminate_at:
            return "terminate"
        if count >= self.limits.loop_notice_at:
            return "notice"
        return None

    def repeats(self) -> int:
        """How many calls were repeats of an earlier identical call."""
        return sum(count - 1 for count in self.seen.values() if count > 1)


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
