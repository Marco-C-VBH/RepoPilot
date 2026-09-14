"""The baseline solver end to end (``-m docker``), with a scripted model.

Real workspace, real sandbox, real harness and runner -- only the model is a
``FakeClient`` that plays the moves a competent agent would make on the fixture
task, so the whole Phase 1 path (tools -> loop -> patch -> verdict -> trace ->
metrics) is exercised without a key or any spend.
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
from evals.solvers import BaselineSolver
from repopilot.agent import AgentBudget
from repopilot.models import FakeClient, ModelResponse, StopReason, ToolCall, Usage
from repopilot.sandbox.docker import remove_image
from repopilot.tracing import Trace
from tests.fixture_repo import create_fixture_repo, fixture_task_dict

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("agent-bench")
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


def competent_script() -> list[ModelResponse]:
    return [
        reply("Reproducing first.", ToolCall("t1", "run_tests", {})),
        reply("", ToolCall("s1", "search_code", {"query": "high - 1"})),
        reply("", ToolCall("r1", "read_file", {"path": "fixturepkg/__init__.py"})),
        reply(
            "",
            ToolCall(
                "e1",
                "edit_file",
                {
                    "path": "fixturepkg/__init__.py",
                    "old_string": "min(value, high - 1)",
                    "new_string": "min(value, high)",
                },
            ),
        ),
        reply("", ToolCall("t2", "run_tests", {"target": "tests/test_clamp.py"})),
        reply("The upper bound was exclusive by one; clamp now uses high directly."),
    ]


def test_baseline_solver_fixes_the_fixture_task(bench: Path, task: Task) -> None:
    solver = BaselineSolver(
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
    assert result.patch_bytes > 0 and result.patch_test_files == []
    assert result.trace_path and Path(result.trace_path).is_file()
    events = Trace.read(Path(result.trace_path))
    assert [e["kind"] for e in events][:3] == ["run_start", "model_call", "tool_call"]
    first_run = next(e for e in events if e["kind"] == "tool_call" and e["name"] == "run_tests")
    assert first_run["meta"]["counts"]["failed"] == 1  # the bug was reproduced before the edit
    assert "## agent" in log and "termination: done" in log


def test_runner_reports_agent_metrics_and_failures(bench: Path, task: Task) -> None:
    scripts = iter(
        [
            competent_script(),  # first task run: PASS
            [reply("I cannot find it, sorry.")],  # second run: no tools, no patch -> FAIL
        ]
    )
    solver = BaselineSolver(
        model="fake-model",
        cache_dir=bench / "cache",
        client_factory=lambda model: FakeClient(model=model, script=next(scripts)),
    )
    summary, results = runner.run_benchmark(
        [task], solver, out_root=bench / "results", repeat=2, cache_dir=bench / "cache"
    )
    assert [r.status for r in results] == [Status.PASS, Status.FAIL]
    assert results[1].failure == "retrieval_failure"
    assert summary.model == "fake-model" and summary.budget is not None
    assert summary.agent is not None
    assert summary.agent["success_rate"] == 0.5 and summary.agent["patch_rate"] == 0.5
    assert summary.agent["terminations"] == {"done": 2}
    assert summary.agent["failures"] == {"retrieval_failure": 1}
    run_dir = Path(summary.results_dir)
    assert (run_dir / "traces" / f"{task.id}.jsonl").is_file()
    assert (run_dir / "traces" / f"{task.id}.1.jsonl").is_file()
    written = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert written["agent"]["success_rate"] == 0.5
    assert summary.deterministic is False  # the two scripted runs differ on purpose
