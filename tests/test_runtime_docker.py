"""The structured solver end to end (``-m docker``), with a scripted model.

Real workspace, real sandbox (the runtime's reproduction and verification runs
happen in the container), real harness and runner -- only the model is a
``FakeClient`` playing the moves of a competent agent on the fixture task.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from evals import runner
from evals.benchmark.registry import load_tasks
from evals.benchmark.schema import Task
from evals.harness import image_spec_for, run_task
from evals.judge import Status
from evals.solvers import StructuredSolver, get_solver
from repopilot.agent import AgentBudget
from repopilot.models import FakeClient, ModelResponse, StopReason, ToolCall, Usage
from repopilot.sandbox.docker import remove_image
from repopilot.tracing import Trace
from tests.fixture_repo import create_fixture_repo, fixture_task_dict

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("runtime-bench")
    sha = create_fixture_repo(root / "repo")
    tasks_dir = root / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "fixture_001.json").write_text(
        json.dumps(fixture_task_dict(root / "repo", sha), indent=2), encoding="utf-8"
    )
    yield root
    remove_image(image_spec_for(load_tasks(tasks_dir)[0]).tag)


@pytest.fixture(scope="module")
def task(bench: Path) -> Task:
    return load_tasks(bench / "tasks")[0]


def reply(text: str = "", *calls: ToolCall) -> ModelResponse:
    return ModelResponse(
        model="fake-model",
        provider="fake",
        text=text,
        tool_calls=tuple(calls),
        stop_reason=StopReason.TOOL_USE if calls else StopReason.END_TURN,
        usage=Usage(500, 50),
        latency_ms=2,
        cost_usd=0.002,
    )


EDIT = ToolCall(
    "e1",
    "edit_file",
    {
        "path": "fixturepkg/__init__.py",
        "old_string": "min(value, high - 1)",
        "new_string": "min(value, high)",
    },
)


def competent_script() -> list[ModelResponse]:
    return [
        reply(json.dumps({"plan": ["read clamp", "fix the bound"], "suspects": ["fixturepkg"]})),
        reply("", ToolCall("s1", "search_code", {"query": "high - 1"})),
        reply("", ToolCall("r1", "read_file", {"path": "fixturepkg/__init__.py"})),
        reply("fixturepkg/__init__.py clamp: the upper bound is exclusive by one."),
        reply("", EDIT),
        reply("clamp now uses high directly."),
    ]


def test_structured_solver_fixes_the_fixture_task(bench: Path, task: Task) -> None:
    solver = StructuredSolver(
        model="fake-model",
        budget=AgentBudget(max_cost_usd=1.0),
        cache_dir=bench / "cache",
        client_factory=lambda model: FakeClient(model=model, script=competent_script()),
    )
    result, log = run_task(task, solver, cache_dir=bench / "cache", trace_dir=bench / "traces")

    assert result.status is Status.PASS, log
    assert result.agent is not None
    assert result.agent["termination"] == "done"
    assert result.agent["steps"] == 6 and result.agent["test_runs"] == 2
    assert result.agent["files_edited"] == ["fixturepkg/__init__.py"]
    runtime = result.agent["runtime"]
    assert runtime["verified"] is True and runtime["initial_failures"] >= 1
    assert runtime["steps_by_phase"] == {"plan": 1, "localize": 3, "patch": 2}
    assert result.trace_path and Path(result.trace_path).is_file()
    events = Trace.read(Path(result.trace_path))
    kinds = [e["kind"] for e in events]
    assert kinds[:3] == ["run_start", "phase", "test_run"]
    reproduce = next(e for e in events if e["kind"] == "test_run")
    assert reproduce["phase"] == "reproduce" and reproduce["failed"] >= 1
    verify = [e for e in events if e["kind"] == "test_run"][-1]
    assert verify["phase"] == "verify" and verify["green"] and verify["fixed"] >= 1
    assert "## agent" in log and "termination: done" in log


def test_runner_reports_runtime_metrics(bench: Path, task: Task) -> None:
    scripts = iter(
        [
            competent_script(),  # first run: PASS
            [
                reply(json.dumps({"plan": ["look"], "suspects": []})),
                reply("It is somewhere in clamp."),
                reply("I would change the bound."),  # PATCH turn without an edit
                reply("Really, the bound."),  # nudged, still no edit -> no_progress
            ],
        ]
    )
    solver = get_solver(
        "structured",
        model="fake-model",
        cache_dir=bench / "cache",
        client_factory=lambda model: FakeClient(model=model, script=next(scripts)),
    )
    summary, results = runner.run_benchmark(
        [task], solver, out_root=bench / "results", repeat=2, cache_dir=bench / "cache"
    )
    assert [r.status for r in results] == [Status.PASS, Status.FAIL]
    assert results[1].agent["termination"] == "no_progress"
    assert results[1].failure == "retrieval_failure"  # nothing edited, gold file never read
    assert summary.model == "fake-model" and summary.agent is not None
    assert summary.agent["terminations"] == {"done": 1, "no_progress": 1}
    assert summary.agent["runtime"]["verified_rate"] == 0.5
    assert summary.agent["runtime"]["nudges"] == 1
    written = json.loads((Path(summary.results_dir) / "summary.json").read_text(encoding="utf-8"))
    assert written["agent"]["runtime"]["results"] == 2
