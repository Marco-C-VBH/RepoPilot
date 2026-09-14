"""The baseline loop, budgets and traces, driven by scripted model replies.

``FakeClient`` plays the model, a temp git repo is the workspace and
``FakeSandbox`` answers ``run_tests``; nothing here needs Docker or a key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repopilot.agent import (
    AgentBudget,
    BaselineAgent,
    BudgetTracker,
    TaskInput,
    Termination,
    task_prompt,
)
from repopilot.models import (
    FakeClient,
    Ledger,
    ModelResponse,
    Role,
    StopReason,
    ToolCall,
    Usage,
)
from repopilot.tools import Toolbox, Workspace
from repopilot.tracing import Trace
from tests.fakes import DEMO_FILES, FakeSandbox, make_run
from tests.gitfixtures import init_repo

TASK = TaskInput(
    task_id="demo_001",
    repo="https://github.com/example/demo",
    base_commit="0123456789abcdef0123456789abcdef01234567",
    description="Cache.put keeps one item too many before evicting.",
    test_command="pytest tests/test_cache.py",
)
EDIT = ToolCall(
    "e1",
    "edit_file",
    {
        "path": "demo/cache.py",
        "old_string": "if len(self.items) > self.size:",
        "new_string": "if len(self.items) >= self.size:",
    },
)
READ = ToolCall("r1", "read_file", {"path": "demo/cache.py", "start": 9, "end": 12})
TESTS = ToolCall("t1", "run_tests", {})


@pytest.fixture
def toolbox(tmp_path: Path) -> Toolbox:
    root = tmp_path / "repo"
    init_repo(root, DEMO_FILES)
    run = make_run(**{"tests/test_cache.py::test_put_evicts": "passed"})
    return Toolbox(Workspace.from_repo(root), FakeSandbox(run), test_command=TASK.test_command)


def response(
    text: str = "",
    calls: tuple[ToolCall, ...] = (),
    *,
    stop: StopReason | None = None,
    tokens: int = 100,
    cost: float = 0.001,
) -> ModelResponse:
    return ModelResponse(
        model="fake-model",
        provider="fake",
        text=text,
        tool_calls=calls,
        stop_reason=stop or (StopReason.TOOL_USE if calls else StopReason.END_TURN),
        usage=Usage(tokens, 10),
        latency_ms=3,
        cost_usd=cost,
    )


def test_task_prompt_carries_repo_command_and_report() -> None:
    prompt = task_prompt(TASK)
    assert prompt.startswith(
        "Repository: demo (https://github.com/example/demo) at commit 0123456789ab"
    )
    assert "Test command: pytest tests/test_cache.py" in prompt
    assert prompt.rstrip().endswith("Cache.put keeps one item too many before evicting.")


def test_happy_path_reads_edits_tests_and_stops(toolbox: Toolbox) -> None:
    client = FakeClient(
        script=[
            response("Let me look.", (READ,)),
            response("", (EDIT,)),
            response("", (TESTS,)),
            response("Root cause: eviction happened one insert late. Changed > to >=."),
        ]
    )
    run = BaselineAgent(client, toolbox, AgentBudget()).run(TASK)

    assert run.termination is Termination.DONE
    assert run.steps == 4 and run.tool_calls == 3 and run.invalid_tool_calls == 0
    assert run.test_runs == 1 and run.test_runs_refused == 0
    assert run.files_read == ["demo/cache.py"] and run.files_edited == ["demo/cache.py"]
    assert run.changed_files == ["demo/cache.py"]
    assert "+        if len(self.items) >= self.size:" in run.patch
    assert run.final_text.startswith("Root cause")
    assert run.ledger["cost_usd"] == pytest.approx(0.004)

    # The conversation the model saw: system, task, then alternating turns.
    first, last = client.calls[0], client.calls[-1]
    assert [m.role for m in first.messages] == [Role.SYSTEM, Role.USER]
    assert first.tools and [t.name for t in first.tools][:2] == ["search_code", "search_symbol"]
    roles = [m.role for m in last.messages]
    assert roles == [Role.SYSTEM, Role.USER] + [Role.ASSISTANT, Role.TOOL] * 3
    assert last.messages[3].tool_call_id == "r1"
    assert "demo/cache.py lines 9-12" in last.messages[3].content

    kinds = [e.kind for e in run.trace.events]
    assert kinds == ["run_start"] + ["model_call", "tool_call"] * 3 + [
        "model_call",
        "patch",
        "run_end",
    ]
    end = run.trace.events[-1].data
    assert end["termination"] == "done" and end["budget"]["steps"] == 4
    tool_events = run.trace.of_kind("tool_call")
    assert tool_events[1].data["name"] == "edit_file" and tool_events[1].data["meta"]["line"] == 10
    assert tool_events[2].data["meta"]["counts"]["passed"] == 1
    assert run.trace.events[-2].data["diff"] == run.patch


def test_record_is_json_friendly_and_trace_round_trips(toolbox: Toolbox, tmp_path: Path) -> None:
    client = FakeClient(script=[response("", (EDIT,)), response("done")])
    run = BaselineAgent(client, toolbox, AgentBudget()).run(TASK)
    record = run.to_record()
    assert record["termination"] == "done"
    assert record["patch_bytes"] > 0 and record["files_edited"] == ["demo/cache.py"]
    assert set(record) >= {"steps", "tool_calls", "cost_usd", "input_tokens", "final_text"}

    path = run.trace.write(tmp_path / "traces" / "demo_001.jsonl")
    events = Trace.read(path)
    assert [e["kind"] for e in events] == [e.kind for e in run.trace.events]
    assert events[0]["seq"] == 0 and events[0]["model"] == "fake-model"


def test_invalid_and_unknown_tool_calls_become_error_results(toolbox: Toolbox) -> None:
    bad_json = ToolCall("b1", "read_file", parse_error="arguments are not valid JSON")
    unknown = ToolCall("u1", "delete_everything", {})
    missing = ToolCall("m1", "read_file", {})
    client = FakeClient(script=[response("", (bad_json, unknown, missing)), response("giving up")])
    run = BaselineAgent(client, toolbox, AgentBudget()).run(TASK)

    assert run.termination is Termination.DONE
    assert run.tool_calls == 3 and run.invalid_tool_calls == 3
    results = [m for m in client.calls[1].messages if m.role is Role.TOOL]
    assert all(m.is_error for m in results)
    assert "valid JSON" in results[0].content
    assert "unknown tool" in results[1].content
    assert "missing required argument" in results[2].content
    assert all(e.data["invalid"] for e in run.trace.of_kind("tool_call"))


def test_step_budget_stops_the_loop(toolbox: Toolbox) -> None:
    client = FakeClient(script=[response("", (READ,))] * 10)
    run = BaselineAgent(client, toolbox, AgentBudget(max_steps=3)).run(TASK)
    assert run.termination is Termination.BUDGET_STEPS
    assert run.steps == 3 and run.tool_calls == 3
    assert len(client.script) == 7  # no further model calls were made


def test_tool_call_budget_refuses_extra_calls_then_stops(toolbox: Toolbox) -> None:
    client = FakeClient(script=[response("", (READ, READ, READ)), response("", (READ,))])
    run = BaselineAgent(client, toolbox, AgentBudget(max_tool_calls=2)).run(TASK)
    assert run.termination is Termination.BUDGET_TOOL_CALLS
    assert run.tool_calls == 2
    # the third call in the first step was refused, and no second step happened
    tool_events = run.trace.of_kind("tool_call")
    assert [e.data["is_error"] for e in tool_events] == [False, False, True]
    assert "tool-call budget exhausted" in tool_events[2].data["output"]
    assert run.steps == 1 and len(client.script) == 1


def test_test_run_budget_refuses_but_lets_the_agent_finish(toolbox: Toolbox) -> None:
    client = FakeClient(
        script=[
            response("", (TESTS,)),
            response("", (TESTS,)),
            response("", (EDIT,)),
            response("ok"),
        ]
    )
    run = BaselineAgent(client, toolbox, AgentBudget(max_test_runs=1)).run(TASK)
    assert run.termination is Termination.DONE
    assert run.test_runs == 1 and run.test_runs_refused == 1
    assert run.tool_calls == 2  # the refused run does not count as a call
    refused = run.trace.of_kind("tool_call")[1].data
    assert refused["is_error"] and "test-run budget exhausted" in refused["output"]
    assert run.patch  # the edit after the refusal still landed


def test_cost_and_token_budgets_stop_after_the_offending_call(toolbox: Toolbox) -> None:
    client = FakeClient(script=[response("", (READ,), cost=0.30)] * 5)
    run = BaselineAgent(client, toolbox, AgentBudget(max_cost_usd=0.50)).run(TASK)
    assert run.termination is Termination.BUDGET_COST
    assert run.steps == 2 and run.ledger["cost_usd"] == pytest.approx(0.60)
    assert run.detail == "cost limit reached"

    client = FakeClient(script=[response("", (READ,), tokens=60_000)] * 5)
    run = BaselineAgent(client, toolbox, AgentBudget(max_tokens=100_000)).run(TASK)
    assert run.termination is Termination.BUDGET_TOKENS and run.steps == 2


def test_runtime_budget_uses_the_clock(toolbox: Toolbox) -> None:
    now = [0.0]
    tracker = BudgetTracker(AgentBudget(max_runtime_seconds=10), Ledger(), clock=lambda: now[0])
    assert tracker.exceeded() is None
    now[0] = 11.0
    assert tracker.exceeded() == "runtime"
    assert tracker.to_record()["runtime_seconds"] == 11.0


def test_model_error_truncation_and_refusal_terminate(toolbox: Toolbox) -> None:
    run = BaselineAgent(FakeClient(script=[]), toolbox, AgentBudget()).run(TASK)
    assert run.termination is Termination.MODEL_ERROR and "fake script exhausted" in (
        run.detail or ""
    )
    assert run.trace.of_kind("model_call")[0].data["error"]

    truncated = FakeClient(script=[response("half a thou", stop=StopReason.MAX_TOKENS)])
    run = BaselineAgent(truncated, toolbox, AgentBudget()).run(TASK)
    assert run.termination is Termination.MODEL_TRUNCATED

    refused = FakeClient(script=[response("", stop=StopReason.OTHER)])
    run = BaselineAgent(refused, toolbox, AgentBudget()).run(TASK)
    assert run.termination is Termination.MODEL_STOPPED


def test_budget_replace_validates_fields() -> None:
    budget = AgentBudget().replace(max_steps=5, max_cost_usd=None)
    assert budget.max_steps == 5 and budget.max_cost_usd == 0.5
    with pytest.raises(ValueError, match="unknown budget field"):
        AgentBudget().replace(max_bananas=1)
