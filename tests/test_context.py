"""The compact context strategy (Phase 2b): what the model sees each step.

The working state is rendered from the runtime's own records; the window keeps
whole tool steps; the phase instruction is re-sent.  A scripted run through the
runtime checks the prompt shapes step by step via ``FakeClient.calls``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import runner
from repopilot.agent import AgentBudget, BudgetTracker, StructuredAgent, TaskInput, Termination
from repopilot.agent import state as agent_state
from repopilot.agent.context import (
    COMPACT_NOTE,
    ContextConfig,
    compact_messages,
    failure_lines,
    render_state,
)
from repopilot.agent.state import AgentState, Hypothesis, Phase
from repopilot.models import FakeClient, ModelResponse, Role, StopReason, ToolCall, Usage
from repopilot.models.types import assistant, tool_result, user
from repopilot.tools import Toolbox, Workspace
from tests.fakes import DEMO_FILES, ScriptedSandbox, make_run
from tests.gitfixtures import init_repo

TASK = TaskInput(
    task_id="demo_001",
    repo="https://github.com/example/demo",
    base_commit="0123456789abcdef0123456789abcdef01234567",
    description="Cache.put keeps one item too many before evicting.",
    test_command="pytest tests/test_cache.py",
)
NODEID = "tests/test_cache.py::test_put_evicts"
REPRODUCTION = (
    "pytest tests/test_cache.py: 0 passed, 1 failed, 0 errors, 0 skipped (0.5s)\n"
    f"FAILED {NODEID}\n"
    "  AssertionError: assert None is None\n"
)


def summary(phase: str, *, failed: list[str], fixed: list[str] = (), new: list[str] = ()):
    return agent_state.TestSummary(  # not imported by name: pytest would try to collect it
        step=1,
        phase=phase,
        passed=3,
        failed=len(failed),
        errors=0,
        fixed=list(fixed),
        still_failing=list(failed),
        newly_failing=list(new),
        is_error=False,
        detail=REPRODUCTION if failed else "all passed",
    )


# ---------------------------------------------------------------- rendering


def test_failure_lines_pair_test_ids_with_their_first_message_line() -> None:
    lines = failure_lines(REPRODUCTION, limit=8, chars=200)
    assert lines == [f"- {NODEID} — AssertionError: assert None is None"]
    assert failure_lines("ERROR tests/x.py::t\n", limit=8, chars=200) == ["- tests/x.py::t"]
    long = "FAILED a::b\n  " + "x" * 500 + "\n"
    assert len(failure_lines(long, limit=8, chars=40)[0]) < 80
    many = "".join(f"FAILED a::t{i}\n  boom\n" for i in range(12))
    assert len(failure_lines(many, limit=8, chars=200)) == 8


def test_render_state_carries_task_reads_patch_tests_and_budget() -> None:
    state = AgentState("demo_001", phase=Phase.PATCH)
    state.test_history.append(summary("reproduce", failed=[NODEID]))
    state.initial_failures = [NODEID]
    state.plan = ["read Cache.put", "fix the comparison"]
    state.suspects = ["demo/cache.py"]
    state.hypotheses = [Hypothesis("wrong idea", 2, "rejected"), Hypothesis("> vs >=", 4)]
    state.reads = [("demo/cache.py", 9, 12), ("demo/cache.py", 1, 8), ("tests/test_cache.py", 1, 7)]
    state.searches = ["Cache.put", "evict"]
    tracker = BudgetTracker(AgentBudget())
    tracker.steps, tracker.tool_calls, tracker.test_runs = 5, 4, 1
    diff = "diff --git a/demo/cache.py b/demo/cache.py\n-    if a > b:\n+    if a >= b:\n"

    text = render_state(TASK, state, tracker, ContextConfig(mode="compact"), diff=diff)
    assert text.startswith("WORKING STATE (step 5, phase patch)")
    assert "Repository: demo (https://github.com/example/demo) at commit 0123456789ab" in text
    assert "Bug report:\nCache.put keeps one item too many before evicting." in text
    assert "Initial test run (before any change): 1 failed, 3 passed. Failing:\n- " + NODEID in text
    assert "Plan:\n1. read Cache.put\n2. fix the comparison" in text
    assert "Suspects: demo/cache.py" in text
    assert "- [rejected] wrong idea\n- [active] > vs >=" in text
    assert "Files read (line ranges" in text
    assert "demo/cache.py 9-12, 1-8; tests/test_cache.py 1-7" in text
    assert "Searches made: 'Cache.put', 'evict'" in text
    assert "Current patch (diff against the original tree):\n" + diff.strip() in text
    assert "Latest test run with your change: not run yet." in text
    assert (
        "Budget left: 25 of 30 steps, 36 of 40 tool calls, 4 of 5 test runs, ~100k of 100k tokens."
        in text
    )

    # After a verification: fixed / still / new, and a truncated long diff.
    state.test_history.append(
        summary("verify", failed=["tests/x.py::t"], fixed=[NODEID], new=["tests/x.py::t"])
    )
    config = ContextConfig(mode="compact", patch_chars=30)
    text = render_state(TASK, state, tracker, config, diff=diff * 3)
    assert "fixed 1 of 1 initially failing, 1 still failing, 1 newly failing." in text
    assert "[diff truncated at 30 chars]" in text

    # A suite that was green from the start says so; no reads, no patch -> the short forms.
    green = AgentState("demo_001")
    green.test_history.append(summary("reproduce", failed=[]))
    text = render_state(TASK, green, BudgetTracker(AgentBudget()), config, diff="")
    assert "all 3 tests passed before any change" in text
    assert "Current patch: none yet." in text and "Files read" not in text


def test_compact_messages_keep_whole_recent_steps_and_resend_the_instruction() -> None:
    history = [
        user("plan prompt"),  # step 0, dropped
        assistant("", (ToolCall("a", "read_file", {"path": "x"}),)),  # step 1
        tool_result("a", "x contents"),  # step 1
        user("Phase: LOCALIZE ..."),  # instruction, dropped
        assistant("", (ToolCall("b", "read_file", {"path": "y"}),)),  # step 2
        tool_result("b", "y contents"),
        user("nudge text"),  # nudge at step 2, kept while in the window
        assistant("hypothesis"),  # step 3
        assistant("", (ToolCall("c", "edit_file", {"path": "y"}),)),  # step 4
        tool_result("c", "edited"),
    ]
    meta = [
        (0, "prompt"),
        (1, "assistant"),
        (1, "tool"),
        (1, "instruction"),
        (2, "assistant"),
        (2, "tool"),
        (2, "nudge"),
        (3, "assistant"),
        (4, "assistant"),
        (4, "tool"),
    ]
    messages = compact_messages(
        system_prompt="SYS",
        state_text="STATE",
        instruction="Phase: PATCH ...",
        history=history,
        meta=meta,
        current_step=5,
        window_steps=3,
    )
    assert messages[0].role is Role.SYSTEM and messages[0].content == "SYS" + COMPACT_NOTE
    assert messages[1].content == "STATE"
    kept = [m.content or (m.tool_calls[0].id if m.tool_calls else "?") for m in messages[2:-1]]
    assert kept == ["b", "y contents", "nudge text", "c", "edited"]  # steps 2-4, decisions dropped
    assert "x contents" not in [m.content for m in messages]  # step 1 fell out of the window
    assert messages[-1].role is Role.USER and messages[-1].content == "Phase: PATCH ..."

    # No window yet (first call of a run): state and instruction share one user turn.
    first = compact_messages(
        system_prompt="SYS",
        state_text="STATE",
        instruction="Phase: PLAN",
        history=history[:1],
        meta=meta[:1],
        current_step=1,
        window_steps=3,
    )
    assert [m.role for m in first] == [Role.SYSTEM, Role.USER]
    assert first[1].content == "STATE\n\nPhase: PLAN"


def test_context_config_rejects_unknown_modes() -> None:
    with pytest.raises(ValueError, match="context mode"):
        ContextConfig(mode="summarised")
    assert ContextConfig().compact is False and ContextConfig(mode="compact").compact is True


# ----------------------------------------------------------- through the runtime

SEARCH = ToolCall("s1", "search_symbol", {"name": "Cache.put"})
READ = ToolCall("r1", "read_file", {"path": "demo/cache.py", "start": 9, "end": 12})
READ2 = ToolCall("r2", "read_file", {"path": "demo/util.py"})
EDIT = ToolCall(
    "e1",
    "edit_file",
    {
        "path": "demo/cache.py",
        "old_string": "if len(self.items) > self.size:",
        "new_string": "if len(self.items) >= self.size:",
    },
)
PLAN = json.dumps({"plan": ["read Cache.put", "fix"], "suspects": ["demo/cache.py"]})


def response(text: str = "", calls: tuple[ToolCall, ...] = ()) -> ModelResponse:
    return ModelResponse(
        model="fake-model",
        provider="fake",
        text=text,
        tool_calls=calls,
        stop_reason=StopReason.TOOL_USE if calls else StopReason.END_TURN,
        usage=Usage(100, 10),
        latency_ms=3,
        cost_usd=0.001,
    )


def test_compact_runtime_sends_state_window_and_instruction_each_step(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, DEMO_FILES)
    failing = make_run(**{NODEID: "failed"})
    passing = make_run(**{NODEID: "passed"})
    toolbox = Toolbox(
        Workspace.from_repo(root),
        ScriptedSandbox(run=passing, runs=[failing, passing]),
        test_command=TASK.test_command,
    )
    client = FakeClient(
        script=[
            response(PLAN),  # 1 PLAN
            response("", (SEARCH,)),  # 2 LOCALIZE
            response("", (READ,)),  # 3
            response("", (READ2,)),  # 4
            response("Cache.put: > should be >=."),  # 5 hypothesis
            response("", (EDIT,)),  # 6 PATCH
            response("changed the comparison"),  # 7 -> TEST green -> DONE
        ]
    )
    agent = StructuredAgent(
        client, toolbox, AgentBudget(), context=ContextConfig(mode="compact", window_steps=2)
    )
    run = agent.run(TASK)
    assert run.termination is Termination.DONE and run.runtime["context"]["mode"] == "compact"

    calls = client.calls
    # Step 1 (PLAN): system + one user turn holding the state and the PLAN instructions.
    first = calls[0].messages
    assert [m.role for m in first] == [Role.SYSTEM, Role.USER]
    assert first[0].content.endswith(COMPACT_NOTE)
    assert "WORKING STATE (step 1, phase plan)" in first[1].content
    assert "Initial test run (before any change): 1 failed" in first[1].content
    assert first[1].content.rstrip().endswith("look at first.")  # PLAN_INSTRUCTIONS
    # Step 2: the LOCALIZE instruction, no tool steps yet in the window.
    second = calls[1].messages
    assert [m.role for m in second] == [Role.SYSTEM, Role.USER]
    assert "Plan:\n1. read Cache.put" in second[1].content
    assert second[1].content.rstrip().endswith("the change you intend to make.")
    # Step 5 (the hypothesis call): window of 2 keeps steps 3 and 4 (r1, r2), not step 2 (s1).
    fifth = calls[4].messages
    ids = [c.id for m in fifth for c in m.tool_calls]
    assert ids == ["r1", "r2"]
    assert "Searches made: 'Cache.put'" in fifth[1].content
    assert "Files read" in fifth[1].content and "demo/cache.py 9-12" in fifth[1].content
    assert fifth[-1].role is Role.USER and fifth[-1].content.startswith("Phase: LOCALIZE")
    # Step 6 (PATCH): the hypothesis is in the state, the instruction is the PATCH one.
    sixth = calls[5].messages
    assert "- [active] Cache.put: > should be >=." in sixth[1].content
    assert sixth[-1].content.startswith("Phase: PATCH")
    # Step 7: the edit's result is in the window and the diff is in the state.
    seventh = calls[6].messages
    assert any(m.tool_call_id == "e1" for m in seventh)
    assert "Current patch (diff against the original tree):" in seventh[1].content
    assert "+        if len(self.items) >= self.size:" in seventh[1].content
    assert "Budget left: 23 of 30 steps" in seventh[1].content  # step 7 counts as spent
    # Every prompt is bounded: the full history is never sent.
    assert max(len(c.messages) for c in calls) <= 2 + 2 * 3 + 1
    # The trace records the context shape per call; the full history is still complete.
    events = [e for e in run.trace.events if e.kind == "model_call"]
    assert events[0].data["context"] == {
        "mode": "compact",
        "messages": 2,
        "chars": len(first[0].content) + len(first[1].content),
    }
    assert all(e.data["context"]["mode"] == "compact" for e in events)
    assert run.trace.events[0].data["context"]["window_steps"] == 2


def compact_agent(tmp_path: Path, script: list, *, window_steps: int = 2):
    root = tmp_path / "repo"
    init_repo(root, DEMO_FILES)
    toolbox = Toolbox(
        Workspace.from_repo(root),
        ScriptedSandbox(
            run=make_run(**{NODEID: "passed"}),
            runs=[make_run(**{NODEID: "failed"}), make_run(**{NODEID: "passed"})],
        ),
        test_command=TASK.test_command,
    )
    client = FakeClient(script=script)
    context = ContextConfig(mode="compact", window_steps=window_steps)
    return StructuredAgent(client, toolbox, AgentBudget(), context=context), client


def policies(run, name: str) -> list[str]:
    return [
        e.data["policy"]
        for e in run.trace.events
        if e.kind == "tool_call" and e.data["name"] == name
    ]


def test_compact_context_rereads_outside_the_window_are_not_loops(tmp_path: Path) -> None:
    """Issue #8: what the model can no longer see, it may legitimately ask for again."""
    agent, client = compact_agent(
        tmp_path,
        [
            response(PLAN),  # 1
            response("", (READ,)),  # 2 LOCALIZE
            response("", (READ2,)),  # 3
            response("", (SEARCH,)),  # 4
            response("", (READ,)),  # 5 step 2's result left the window: a re-read, executed
            response("", (READ,)),  # 6 step 5's result is in view: a repeat, still executed
            response("", (READ,)),  # 7 the third in view: refused with the notice
            response("", (SEARCH,)),  # 8 re-read (step 4 is out of view)
            response("", (READ2,)),  # 9 re-read (step 3 is out of view)
            response("", (READ,)),  # 10 the notice (step 7) has left the window: executed again
            response("Cache.put: > should be >=."),  # 11 hypothesis
            response("", (EDIT,)),  # 12 PATCH
            response("changed the comparison"),  # 13 -> TEST green -> DONE
        ],
    )
    run = agent.run(TASK)

    assert run.termination is Termination.DONE
    assert policies(run, "read_file") == [
        "executed",  # 2
        "executed",  # 3 (demo/util.py)
        "executed",  # 5
        "executed",  # 6
        "loop_notice",  # 7
        "executed",  # 9 (demo/util.py)
        "executed",  # 10
    ]
    assert policies(run, "search_symbol") == ["executed", "executed"]
    assert run.runtime["loop_interventions"] == 1
    assert run.runtime["repeated_tool_calls"] == 6 and run.runtime["rereads"] == 4
    # The notice was true when it was sent: both earlier results were in the prompt.
    seventh = client.calls[6].messages
    assert [c.id for m in seventh for c in m.tool_calls] == ["r1", "r1"]


def test_compact_context_still_ends_a_loop_the_model_can_see(tmp_path: Path) -> None:
    agent, _ = compact_agent(
        tmp_path,
        [response(PLAN)] + [response("", (READ,))] * 4 + [response("never reached")],
    )
    run = agent.run(TASK)
    assert run.termination is Termination.AGENT_LOOP
    assert policies(run, "read_file") == ["executed", "executed", "loop_notice", "loop_terminated"]
    assert run.runtime["rereads"] == 0


def test_runner_context_flag_reaches_the_structured_solver(monkeypatch: pytest.MonkeyPatch) -> None:
    from evals.solvers import StructuredSolver
    from repopilot.models.config import ConfigError

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    parser = runner.build_parser()
    solver = runner.build_solver(
        parser.parse_args(["--solver", "structured", "--context", "compact", "--env-file", "x"])
    )
    assert isinstance(solver, StructuredSolver) and solver.context == "compact"
    default = runner.build_solver(parser.parse_args(["--solver", "structured", "--env-file", "x"]))
    assert default.context == "full"
    with pytest.raises(ConfigError, match="structured-solver option"):
        runner.build_solver(
            parser.parse_args(["--solver", "baseline", "--context", "compact", "--env-file", "x"])
        )
