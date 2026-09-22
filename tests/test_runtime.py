"""The structured runtime (Phase 2), driven by scripted model replies.

Same stand-ins as the baseline tests -- ``FakeClient`` plays the model, a temp git
repo is the workspace, ``ScriptedSandbox`` answers the runtime's test runs -- so
every transition of the state machine, every intervention and every termination
reason is exercised without Docker or a key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.metrics import Failure, agent_metrics, classify_failure, format_agent_metrics
from repopilot.agent import (
    AgentBudget,
    Phase,
    RuntimeLimits,
    StructuredAgent,
    TaskInput,
    Termination,
)
from repopilot.agent.policies import LoopDetector, call_signature, parse_json_reply
from repopilot.models import FakeClient, ModelResponse, StopReason, ToolCall, Usage
from repopilot.tools import Toolbox, Workspace
from repopilot.tracing import Trace
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
FAILING = make_run(**{NODEID: "failed"})
PASSING = make_run(**{NODEID: "passed"})

SEARCH = ToolCall("s1", "search_symbol", {"name": "Cache.put"})
READ = ToolCall("r1", "read_file", {"path": "demo/cache.py", "start": 9, "end": 12})
EDIT = ToolCall(
    "e1",
    "edit_file",
    {
        "path": "demo/cache.py",
        "old_string": "if len(self.items) > self.size:",
        "new_string": "if len(self.items) >= self.size:",
    },
)
EDIT_AGAIN = ToolCall(
    "e2",
    "edit_file",
    {
        "path": "demo/cache.py",
        "old_string": "DEFAULT_SIZE = 3",
        "new_string": "DEFAULT_SIZE = 4",
    },
)
PLAN = json.dumps({"plan": ["run tests", "read Cache.put", "fix"], "suspects": ["demo/cache.py"]})
HYPOTHESIS = "demo/cache.py Cache.put evicts one insert late: > should be >=."


def response(
    text: str = "",
    calls: tuple[ToolCall, ...] = (),
    *,
    stop: StopReason | None = None,
    tokens: int = 100,
) -> ModelResponse:
    return ModelResponse(
        model="fake-model",
        provider="fake",
        text=text,
        tool_calls=calls,
        stop_reason=stop or (StopReason.TOOL_USE if calls else StopReason.END_TURN),
        usage=Usage(tokens, 10),
        latency_ms=3,
        cost_usd=0.001,
    )


def make_toolbox(tmp_path: Path, *runs) -> Toolbox:
    root = tmp_path / "repo"
    init_repo(root, DEMO_FILES)
    sandbox = ScriptedSandbox(run=PASSING, runs=list(runs) or [FAILING, PASSING])
    return Toolbox(Workspace.from_repo(root), sandbox, test_command=TASK.test_command)


def agent(toolbox: Toolbox, script: list, **kwargs) -> tuple[StructuredAgent, FakeClient]:
    client = FakeClient(script=script)
    return StructuredAgent(client, toolbox, kwargs.pop("budget", AgentBudget()), **kwargs), client


def kinds(trace: Trace) -> list[str]:
    return [e.kind for e in trace.events]


ALL = ["search_code", "search_symbol", "find_references", "read_file", "edit_file"]
SEARCHING = ALL[:4]


def tools_offered(client: FakeClient) -> list[list[str]]:
    """The tools the model could actually call at each step ([] at a decision point)."""
    return [
        [] if call.tool_choice == "none" else [t.name for t in call.tools] for call in client.calls
    ]


# ------------------------------------------------------------------- the happy path


def test_happy_path_walks_every_phase_and_verifies(tmp_path: Path) -> None:
    toolbox = make_toolbox(tmp_path)
    runner, client = agent(
        toolbox,
        [
            response(PLAN),
            response("", (SEARCH,)),
            response("", (READ,)),
            response(HYPOTHESIS),
            response("", (EDIT,)),
            response("Changed > to >= in Cache.put."),
        ],
    )
    run = runner.run(TASK)

    assert run.termination is Termination.DONE and run.detail.startswith("verified")
    assert run.steps == 6 and run.tool_calls == 3 and run.invalid_tool_calls == 0
    assert run.test_runs == 2  # reproduction + one verification
    assert run.files_read == ["demo/cache.py"] and run.files_edited == ["demo/cache.py"]
    assert run.first_edit_file == "demo/cache.py"
    assert run.files_read_before_first_edit == ["demo/cache.py"]
    assert "+        if len(self.items) >= self.size:" in run.patch
    assert run.final_text == "Changed > to >= in Cache.put."

    # The model never sees run_tests; tools follow the phase.  Decision points send the
    # definitions with tool_choice="none" (the history holds tool calls).
    assert tools_offered(client) == [[], SEARCHING, SEARCHING, SEARCHING, ALL, ALL]
    assert [t.name for t in client.calls[0].tools] == ALL
    assert [c.tool_choice for c in client.calls] == ["none", "auto", "auto", "auto", "auto", "auto"]
    # The reproduction run's failure is in the PLAN prompt; the PATCH instructions follow.
    first_prompt = client.calls[0].messages[-1].content
    assert "Initial test run (before any change)" in first_prompt
    assert "1 failed" in first_prompt and NODEID in first_prompt
    assert "Phase: PATCH" in client.calls[4].messages[-1].content

    runtime = run.runtime
    assert runtime is not None and runtime["verified"] is True
    assert runtime["plan"] == ["run tests", "read Cache.put", "fix"]
    assert runtime["suspects"] == ["demo/cache.py"]
    assert runtime["hypotheses"] == [{"step": 4, "status": "active", "text": HYPOTHESIS}]
    assert runtime["initial_failures"] == 1
    assert runtime["steps_by_phase"] == {"plan": 1, "localize": 3, "patch": 2}
    assert [t["to"] for t in runtime["transitions"]] == [
        "initialize",
        "plan",
        "localize",
        "patch",
        "test",
    ]
    assert runtime["steps_after_green"] == 0 and runtime["first_green_step"] == 6
    assert runtime["loop_interventions"] == 0 and runtime["nudges"] == 0
    verify = runtime["test_history"][1]
    assert verify["phase"] == "verify" and verify["green"] and verify["fixed"] == 1

    trace_kinds = kinds(run.trace)
    assert trace_kinds[:4] == ["run_start", "phase", "test_run", "phase"]
    assert trace_kinds[-3:] == ["state", "patch", "run_end"]
    assert trace_kinds.count("decision") == 2  # plan + hypothesis
    events = run.trace.events
    start = events[0].data
    assert start["runtime"] == "structured" and "run_tests" not in start["tools"]
    record = run.to_record()
    assert record["runtime"]["verified"] is True and record["termination"] == "done"


# ------------------------------------------------------------ policies and recovery


def test_tools_outside_the_phase_are_refused_and_counted(tmp_path: Path) -> None:
    toolbox = make_toolbox(tmp_path)
    tests_call = ToolCall("t1", "run_tests", {})
    runner, client = agent(
        toolbox,
        [
            response(PLAN),
            response("", (EDIT,)),  # editing while localizing: refused
            response(HYPOTHESIS),
            response("", (tests_call, EDIT)),  # run_tests never available; the edit runs
            response("done"),
        ],
    )
    run = runner.run(TASK)

    assert run.termination is Termination.DONE
    assert run.runtime["refused_tool_calls"] == 2 and run.invalid_tool_calls == 0
    assert run.tool_calls == 3  # refused calls still count as requests
    refusals = [e.data for e in run.trace.events if e.data.get("policy") == "phase_refused"]
    assert [r["name"] for r in refusals] == ["edit_file", "run_tests"]
    assert "not available while localizing" in refusals[0]["output"]
    assert "runtime runs the full test command itself" in refusals[1]["output"]
    # The refused edit did not touch the file.
    assert "+        if len(self.items) >= self.size:" in run.patch
    assert run.files_edited == ["demo/cache.py"]


def test_failing_verification_goes_through_analyze_and_can_revert(tmp_path: Path) -> None:
    toolbox = make_toolbox(tmp_path, FAILING, FAILING, PASSING)
    analysis = json.dumps({"diagnosis": "wrong constant", "next": "patch", "keep_patch": False})
    runner, client = agent(
        toolbox,
        [
            response(PLAN),
            response("wrong idea: the default size"),  # hypothesis without any search
            response("", (EDIT_AGAIN,)),
            response("bumped the default"),
            response(analysis),  # ANALYZE: revert and patch again
            response("", (EDIT,)),
            response("fixed the comparison"),
        ],
    )
    run = runner.run(TASK)

    assert run.termination is Termination.DONE and run.test_runs == 3
    assert run.runtime["analyze_rounds"] == 1 and run.runtime["workspace_resets"] == 1
    assert "DEFAULT_SIZE = 4" not in run.patch and ">= self.size" in run.patch
    assert [h["status"] for h in run.runtime["hypotheses"]] == ["tested"]
    # The ANALYZE prompt carried the runtime's own reading of the failure.
    analyze_prompt = client.calls[4].messages[-1].content
    assert analyze_prompt.startswith("Phase: ANALYZE")
    assert "fixed 0 of 1 initially failing" in analyze_prompt
    # ... and the PATCH instructions that followed mention the revert.
    assert client.calls[5].messages[-1].content.startswith("Your change has been reverted")
    decisions = [e.data for e in run.trace.events if e.kind == "decision"]
    assert decisions[-1]["name"] == "analyze" and decisions[-1]["keep_patch"] is False
    assert [t["to"] for t in run.runtime["transitions"]][-4:] == [
        "test",
        "analyze",
        "patch",
        "test",
    ]


def test_analyze_can_send_the_model_back_to_localize(tmp_path: Path) -> None:
    toolbox = make_toolbox(tmp_path, FAILING, FAILING, PASSING)
    runner, client = agent(
        toolbox,
        [
            response(PLAN),
            response("first guess"),
            response("", (EDIT_AGAIN,)),
            response("edited"),
            response(json.dumps({"diagnosis": "elsewhere", "next": "localize"})),
            response("", (READ,)),
            response(HYPOTHESIS),
            response("", (EDIT,)),
            response("edited again"),
        ],
    )
    run = runner.run(TASK)

    assert run.termination is Termination.DONE
    assert [h["status"] for h in run.runtime["hypotheses"]] == ["rejected", "active"]
    assert run.runtime["workspace_resets"] == 0  # keep_patch defaulted to true
    assert "DEFAULT_SIZE = 4" in run.patch and ">= self.size" in run.patch
    assert tools_offered(client)[4] == [] and tools_offered(client)[5] == SEARCHING


def test_loop_detection_notices_then_terminates(tmp_path: Path) -> None:
    toolbox = make_toolbox(tmp_path)
    runner, _ = agent(
        toolbox,
        [
            response(PLAN),
            response("", (SEARCH,)),
            response("", (SEARCH,)),
            response("", (SEARCH,)),  # third identical call: refused with a notice
            response("", (SEARCH,)),  # fourth: the run ends
            response("never reached"),
        ],
    )
    run = runner.run(TASK)

    assert run.termination is Termination.AGENT_LOOP
    assert run.runtime["loop_interventions"] == 1
    assert run.runtime["repeated_tool_calls"] == 3
    policies = [e.data["policy"] for e in run.trace.events if e.kind == "tool_call"]
    assert policies == ["executed", "executed", "loop_notice", "loop_terminated"]
    notice = [e for e in run.trace.events if e.data.get("policy") == "loop_notice"][0]
    assert "already made this exact search_symbol call" in notice.data["output"]
    assert run.patch == ""


def test_loop_detection_applies_to_refused_calls_too(tmp_path: Path) -> None:
    toolbox = make_toolbox(tmp_path)
    runner, _ = agent(
        toolbox,
        [response(PLAN)] + [response("", (EDIT,))] * 4 + [response("never")],
    )
    run = runner.run(TASK)
    assert run.termination is Termination.AGENT_LOOP
    policies = [e.data["policy"] for e in run.trace.events if e.kind == "tool_call"]
    assert policies == ["phase_refused", "phase_refused", "loop_notice", "loop_terminated"]


def test_the_same_read_after_an_edit_or_a_reset_is_not_a_repeat(tmp_path: Path) -> None:
    """Issue #8: the workspace changed, so the same call is a new question."""
    toolbox = make_toolbox(tmp_path, FAILING, FAILING, PASSING)
    revert = json.dumps({"diagnosis": "wrong constant", "next": "patch", "keep_patch": False})
    runner, _ = agent(
        toolbox,
        [
            response(PLAN),
            response("", (READ,)),  # LOCALIZE: the read, original tree
            response(HYPOTHESIS),
            response("", (READ, EDIT_AGAIN)),  # PATCH: the same read (a repeat), then an edit
            response("bumped the default"),  # -> TEST fails
            response(revert),  # ANALYZE: the workspace is reset
            response("", (READ, EDIT)),  # PATCH: the same read of a changed tree: not a third one
            response("fixed the comparison"),  # -> TEST green -> DONE
        ],
    )
    run = runner.run(TASK)

    assert run.termination is Termination.DONE and run.runtime["workspace_resets"] == 1
    reads = [
        e.data["policy"]
        for e in run.trace.events
        if e.kind == "tool_call" and e.data["name"] == "read_file"
    ]
    assert reads == ["executed", "executed", "executed"]
    assert run.runtime["repeated_tool_calls"] == 1 and run.runtime["loop_interventions"] == 0
    assert run.runtime["rereads"] == 0  # the full history never drops a result


def test_patch_turn_without_an_edit_is_nudged_then_ends_as_no_progress(tmp_path: Path) -> None:
    toolbox = make_toolbox(tmp_path)
    runner, client = agent(
        toolbox,
        [
            response(PLAN),
            response(HYPOTHESIS),
            response("The fix is to use >= instead of >."),  # talks, does not edit
            response("I am confident the fix is >=."),  # still no edit
            response("never reached"),
        ],
    )
    run = runner.run(TASK)

    assert run.termination is Termination.NO_PROGRESS
    assert run.runtime["nudges"] == 1 and run.patch == ""
    assert client.calls[3].messages[-1].content.startswith("No file has changed")
    assert run.steps == 4 and run.test_runs == 1  # nothing to verify


def test_localize_tool_limit_forces_the_hypothesis(tmp_path: Path) -> None:
    toolbox = make_toolbox(tmp_path)
    limits = RuntimeLimits(localize_tool_calls=2)
    runner, client = agent(
        toolbox,
        [
            response(PLAN),
            response("", (SEARCH, READ)),  # two calls in one turn: the limit is reached
            response(HYPOTHESIS),  # asked for the hypothesis, no tools offered
            response("", (EDIT,)),
            response("done"),
        ],
        limits=limits,
    )
    run = runner.run(TASK)

    assert run.termination is Termination.DONE
    assert run.runtime["forced_transitions"] == 1
    assert tools_offered(client)[2] == []
    assert client.calls[2].messages[-1].content.startswith("The tool-call limit for this phase")
    forced = [e for e in run.trace.events if e.data.get("name") == "hypothesis_requested"]
    assert forced and forced[0].data["calls"] == 2


def test_patch_edit_limit_refuses_further_edits_and_the_runtime_tests(tmp_path: Path) -> None:
    """Once a PATCH visit's edits are spent and the tree changed, the runtime runs
    the tests itself rather than wait for a turn the model may never end (issue
    #11's trace: eight steps of refused edits up to the token cap)."""
    toolbox = make_toolbox(tmp_path)
    runner, client = agent(
        toolbox,
        [
            response(PLAN),
            response(HYPOTHESIS),
            response("", (EDIT, EDIT_AGAIN)),  # second edit exceeds the cap of 1
            response("never reached: the runtime tested after the edit"),
        ],
        limits=RuntimeLimits(patch_edits=1),
    )
    run = runner.run(TASK)
    assert run.termination is Termination.DONE and run.steps == 3
    policies = [e.data["policy"] for e in run.trace.events if e.kind == "tool_call"]
    assert policies == ["executed", "edit_limit"]
    assert "DEFAULT_SIZE = 4" not in run.patch and run.runtime["refused_tool_calls"] == 1
    assert run.runtime["forced_transitions"] == 1 and run.runtime["verified"] is True
    assert [e.data["name"] for e in run.trace.events if e.kind == "intervention"] == ["edits_spent"]
    assert run.runtime["notes"] == [
        "step 3: the 1 edits of this PATCH visit were used, so the runtime ran the tests."
    ]
    assert len(client.calls) == 3  # the fourth scripted reply was never requested

    # Spending the cap on an edit and its own undo leaves the tree as it was last
    # tested: nothing to run, so the model must end its turn as before.
    toolbox = make_toolbox(tmp_path / "again", FAILING, FAILING, PASSING)
    undo = ToolCall(
        "u1",
        "edit_file",
        {
            "path": "demo/cache.py",
            "old_string": "DEFAULT_SIZE = 4",
            "new_string": "DEFAULT_SIZE = 3",
        },
    )
    runner, _ = agent(
        toolbox,
        [
            response(PLAN),
            response(HYPOTHESIS),
            response("", (EDIT,)),
            response("first try"),  # -> TEST (fails)
            response(json.dumps({"diagnosis": "x", "next": "patch", "keep_patch": True})),
            response("", (EDIT_AGAIN, undo)),  # the cap of 2, but the diff is the tested one
            response("nothing new"),  # -> nudged
            response("still nothing"),  # -> no_progress
        ],
        limits=RuntimeLimits(patch_edits=2),
    )
    run = runner.run(TASK)
    assert run.termination is Termination.NO_PROGRESS
    assert run.runtime["forced_transitions"] == 0 and run.runtime["nudges"] == 1
    assert run.test_runs == 2 and "DEFAULT_SIZE = 4" not in run.patch


def test_empty_reply_is_nudged_once(tmp_path: Path) -> None:
    toolbox = make_toolbox(tmp_path)
    runner, client = agent(
        toolbox,
        [
            response(PLAN),
            response(""),  # empty hypothesis
            response(HYPOTHESIS),
            response("", (EDIT,)),
            response("done"),
        ],
    )
    run = runner.run(TASK)
    assert run.termination is Termination.DONE and run.runtime["nudges"] == 1
    assert (
        client.calls[2].messages[-1].content
        == "Your reply was empty. Reply as instructed for the current phase."
    )


# ------------------------------------------------------ hidden-only tasks, budgets


def test_green_suite_at_start_goes_through_finalize(tmp_path: Path) -> None:
    toolbox = make_toolbox(tmp_path, PASSING, PASSING, PASSING)
    runner, client = agent(
        toolbox,
        [
            response(PLAN),
            response(HYPOTHESIS),
            response("", (EDIT_AGAIN,)),
            response("bumped"),
            response(json.dumps({"decision": "continue", "reason": "report says eviction"})),
            response(HYPOTHESIS),
            response("", (EDIT,)),
            response("fixed"),
            response(json.dumps({"decision": "continue", "reason": "still unsure"})),  # ignored
        ],
    )
    run = runner.run(TASK)

    assert run.termination is Termination.DONE
    assert "passes before any change" in client.calls[0].messages[-1].content
    assert run.runtime["finalize_continues"] == 1 and run.runtime["initial_failures"] == 0
    assert [t["to"] for t in run.runtime["transitions"]] == [
        "initialize",
        "plan",
        "localize",
        "patch",
        "test",
        "finalize",
        "localize",
        "patch",
        "test",
        "finalize",
    ]
    assert client.calls[5].messages[-1].content.startswith("You chose to continue")
    # "green" is no success signal when the suite started green: continues are counted
    # by finalize_continues, not as steps after green.
    assert run.runtime["steps_after_green"] == 0 and run.runtime["first_green_step"] is None
    assert run.runtime["verified"] is True


def test_test_run_budget_ends_the_run_with_the_patch_kept(tmp_path: Path) -> None:
    toolbox = make_toolbox(tmp_path)
    runner, _ = agent(
        toolbox,
        [response(PLAN), response(HYPOTHESIS), response("", (EDIT,)), response("done")],
        budget=AgentBudget(max_test_runs=1),  # the reproduction run uses it up
    )
    run = runner.run(TASK)
    assert run.termination is Termination.BUDGET_TEST_RUNS
    assert run.termination.is_budget and run.test_runs == 1
    assert ">= self.size" in run.patch and run.runtime["verified"] is False
    refused = [e for e in run.trace.events if e.kind == "test_run" and e.data.get("refused")]
    assert len(refused) == 1


def test_edit_in_the_reply_that_crosses_the_token_cap_is_still_applied(tmp_path: Path) -> None:
    """Issue #7: tenacity_004 (Haiku) ended as budget_tokens on the very reply that
    carried the correct edit_file call, and the edit never reached the workspace."""
    toolbox = make_toolbox(tmp_path)
    runner, _ = agent(
        toolbox,
        [
            response(PLAN, tokens=100),
            response(HYPOTHESIS, tokens=100),
            response("", (EDIT,), tokens=1000),  # this reply crosses the cap
            response("never reached"),
        ],
        budget=AgentBudget(max_tokens=900),
    )
    run = runner.run(TASK)

    assert run.termination is Termination.BUDGET_TOKENS
    assert ">= self.size" in run.patch  # the edit was executed before the termination
    assert run.files_edited == ["demo/cache.py"] and run.test_runs == 1  # no verification
    assert run.runtime["verified"] is False
    policies = [e.data["policy"] for e in run.trace.events if e.kind == "tool_call"]
    assert policies == ["executed"]

    # A reply without tool calls that crosses the cap terminates at once, as before.
    toolbox = make_toolbox(tmp_path / "b")
    runner, _ = agent(
        toolbox,
        [response(PLAN, tokens=100), response(HYPOTHESIS, tokens=1000), response("never")],
        budget=AgentBudget(max_tokens=900),
    )
    run = runner.run(TASK)
    assert run.termination is Termination.BUDGET_TOKENS and run.steps == 2


def test_step_budget_is_checked_before_every_model_call(tmp_path: Path) -> None:
    toolbox = make_toolbox(tmp_path)
    runner, _ = agent(
        toolbox,
        [response(PLAN), response("", (SEARCH,)), response("never")],
        budget=AgentBudget(max_steps=2),
    )
    run = runner.run(TASK)
    assert run.termination is Termination.BUDGET_STEPS and run.steps == 2
    assert run.runtime["phase"] == "localize"


def test_unparsable_plan_and_analysis_fall_back_to_defaults(tmp_path: Path) -> None:
    toolbox = make_toolbox(tmp_path, FAILING, FAILING, PASSING)
    runner, _ = agent(
        toolbox,
        [
            response("I will look at Cache.put first."),  # not JSON
            response(HYPOTHESIS),
            response("", (EDIT_AGAIN,)),
            response("edited"),
            response("Hmm, the comparison must be wrong."),  # not JSON: patch, keep
            response("", (EDIT,)),
            response("edited again"),
        ],
    )
    run = runner.run(TASK)
    assert run.termination is Termination.DONE
    assert run.runtime["plan"] == ["I will look at Cache.put first."]
    decisions = [e.data for e in run.trace.events if e.kind == "decision"]
    assert decisions[0]["parsed"] is False
    analyze = [d for d in decisions if d["name"] == "analyze"][0]
    assert analyze["parsed"] is False and analyze["next"] == "patch" and analyze["keep_patch"]


def test_model_error_and_provider_stop_terminate(tmp_path: Path) -> None:
    toolbox = make_toolbox(tmp_path)
    runner, _ = agent(toolbox, [response(PLAN)])  # script exhausted -> ModelError
    run = runner.run(TASK)
    assert run.termination is Termination.MODEL_ERROR and run.steps == 2

    toolbox = make_toolbox(tmp_path / "b")
    runner, _ = agent(toolbox, [response(PLAN), response("", stop=StopReason.OTHER)])
    run = runner.run(TASK)
    assert run.termination is Termination.MODEL_STOPPED


# --------------------------------------------------------------- policies, metrics


def test_loop_detector_and_json_parsing() -> None:
    detector = LoopDetector(RuntimeLimits(loop_notice_at=2))
    assert detector.check("read_file", {"path": "a", "start": 1}, step=1) is None
    assert detector.check("read_file", {"start": 1, "path": "a"}, step=2) == "notice"  # same call
    assert detector.check("read_file", {"path": "a", "start": 1}, step=3) == "terminate"
    assert detector.check("read_file", {"path": "b"}, step=3) is None
    assert detector.repeats() == 2
    # After an edit or a reset (a new workspace version) the same call is a new one.
    assert detector.check("read_file", {"path": "a", "start": 1}, version=1, step=4) is None
    assert detector.repeats() == 2 and detector.rereads == 0
    assert call_signature("x", None) == "x {}"

    # In a compact context only what the model can still see counts (issue #8):
    # an occurrence that left the window makes the call a re-read, not a loop ...
    window = LoopDetector(RuntimeLimits(loop_notice_at=2))
    assert window.check("read_file", {"path": "a"}, step=1, since=0) is None
    assert window.check("read_file", {"path": "a"}, step=5, since=3) is None
    assert window.rereads == 1 and window.repeats() == 1
    # ... two in view earn the notice, a repeat while the notice is in view ends
    # the run, and once the notice itself has left the window it is a re-read again.
    assert window.check("read_file", {"path": "a"}, step=6, since=4) == "notice"
    assert window.check("read_file", {"path": "a"}, step=8, since=6) == "terminate"
    later = LoopDetector(RuntimeLimits(loop_notice_at=2))
    assert later.check("read_file", {"path": "a"}, step=1, since=0) is None
    assert later.check("read_file", {"path": "a"}, step=2, since=0) == "notice"
    assert later.check("read_file", {"path": "a"}, step=7, since=5) is None
    assert later.rereads == 1

    assert parse_json_reply('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_reply('Sure: {"plan": ["x"]} done') == {"plan": ["x"]}
    assert parse_json_reply("[1, 2]") is None and parse_json_reply("") is None
    assert parse_json_reply("{not json}") is None


def test_runtime_metrics_and_loop_failure(tmp_path: Path) -> None:
    from evals.benchmark.schema import Task
    from evals.harness import TaskResult
    from evals.judge import Reason, Status

    example = Path(__file__).resolve().parent.parent / "evals/benchmark/examples/example_000.json"
    task = Task.model_validate(json.loads(example.read_text(encoding="utf-8")))

    def result(status: Status, runtime: dict, termination: str = "done") -> TaskResult:
        return TaskResult(
            task_id=task.id,
            solver="structured",
            status=status,
            reasons=[] if status is Status.PASS else [Reason.FAIL_TO_PASS_FAILING],
            started_at="2026-09-15T00:00:00+00:00",
            durations={"total": 20.0, "solve": 15.0},
            patch_bytes=10,
            agent={
                "termination": termination,
                "steps": 6,
                "tool_calls": 3,
                "invalid_tool_calls": 0,
                "test_runs": 2,
                "cost_usd": 0.05,
                "input_tokens": 1000,
                "output_tokens": 100,
                "files_read": [],
                "files_edited": [],
                "changed_files": [],
                "runtime": runtime,
            },
        )

    clean = {
        "verified": True,
        "loop_interventions": 0,
        "forced_transitions": 0,
        "nudges": 0,
        "refused_tool_calls": 0,
        "workspace_resets": 0,
        "steps_after_green": 0,
        "initial_failures": 2,
        "steps_by_phase": {"plan": 1, "localize": 3, "patch": 2},
    }
    looped = {**clean, "verified": False, "loop_interventions": 1, "repeated_tool_calls": 3}
    results = [result(Status.PASS, clean), result(Status.FAIL, looped, "agent_loop")]
    assert classify_failure(task, results[1]) is Failure.AGENT_LOOP

    metrics = agent_metrics([task], results)
    assert metrics is not None and metrics["runtime"]["results"] == 2
    assert metrics["runtime"]["verified_rate"] == 0.5 and metrics["runtime"]["loop_rate"] == 0.5
    assert metrics["runtime"]["steps_by_phase"] == {"localize": 6, "patch": 4, "plan": 2}
    assert metrics["runtime"]["repeated_tool_calls"] == 3
    assert metrics["failures"] == {"agent_loop": 1}
    text = format_agent_metrics(metrics)
    assert "runtime: verified 50.0% · loop rate 50.0%" in text and "steps by phase" in text


def test_phase_enum_and_state_record_shape(tmp_path: Path) -> None:
    toolbox = make_toolbox(tmp_path)
    runner, _ = agent(
        toolbox,
        [response(PLAN), response(HYPOTHESIS), response("", (EDIT,)), response("done")],
    )
    run = runner.run(TASK)
    state = [e for e in run.trace.events if e.kind == "state"][0].data
    assert state["phase"] == str(Phase.TEST) and state["visited_files"] == ["demo/cache.py"]
    assert state["initial_failing_tests"] == [NODEID]
    assert len(state["transitions"]) == 5
    assert pytest.approx(run.ledger["cost_usd"]) == 0.004


def test_analyze_prompt_states_progress_before_offering_the_revert(tmp_path: Path) -> None:
    """2a.1: cachetools_003 -- a patch that fixed the initial failure and broke another test."""
    other = "tests/test_cache.py::test_missing_getsizeof"
    regressed = make_run(**{NODEID: "passed", other: "failed"})
    toolbox = make_toolbox(tmp_path, FAILING, regressed, PASSING)
    runner, client = agent(
        toolbox,
        [
            response(PLAN),
            response(HYPOTHESIS),
            response("", (EDIT,)),
            response("edited"),
            response(json.dumps({"diagnosis": "guard missing", "next": "patch"})),
            response("", (EDIT_AGAIN,)),
            response("guarded"),
        ],
    )
    run = runner.run(TASK)
    assert run.termination is Termination.DONE
    prompt = client.calls[4].messages[-1].content
    assert "fixed 1 of the 1 initially failing test(s) and broke 1" in prompt
    assert "Reverting discards that progress" in prompt
    assert "Newly failing: " + other in prompt
    assert run.runtime["workspace_resets"] == 0
    assert run.edits == 2 and run.failed_edits == 0 and run.tolerant_edits == 0
    assert run.to_record()["edits"] == 2
