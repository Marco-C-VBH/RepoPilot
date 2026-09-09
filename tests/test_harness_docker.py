"""Phase 0 exit criterion, end to end against Docker (``-m docker``).

A one-task benchmark is written to a temp directory from the fixture project,
then driven through the real CLI: the ``null`` oracle must FAIL it, the ``gold``
oracle must PASS it twice with identical outcomes, and wrong / unappliable /
crashing solvers must produce their specific reason codes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from evals import runner
from evals.benchmark.registry import load_tasks
from evals.benchmark.schema import Task
from evals.harness import evaluate_patch, image_spec_for, run_task
from evals.judge import Reason, Status
from repopilot.sandbox.docker import build_task_image, remove_image
from tests.fixture_repo import (
    ABOVE,
    BELOW,
    GOLD_PATCH,
    HIDDEN,
    INSIDE,
    UNAPPLIABLE_PATCH,
    WRONG_PATCH,
    create_fixture_repo,
    fixture_task_dict,
)

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("bench")
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


@pytest.fixture(scope="module")
def image(bench: Path, task: Task) -> str:
    return build_task_image(image_spec_for(task), cache_dir=bench / "cache")


def cli(bench: Path, out: str, *args: str) -> tuple[int, Path]:
    code = runner.main(
        [
            "--tasks",
            str(bench / "tasks"),
            "--out",
            str(bench / out),
            "--cache-dir",
            str(bench / "cache"),
            *args,
        ]
    )
    run_dirs = sorted(p for p in (bench / out).iterdir() if p.is_dir())
    assert len(run_dirs) == 1, run_dirs
    return code, run_dirs[0]


def test_null_oracle_fails_every_task(bench: Path) -> None:
    code, run_dir = cli(bench, "results-null", "--solver", "null", "--expect", "fail")
    assert code == 0

    (result,) = runner.load_results(run_dir / "results.jsonl")
    assert result.status is Status.FAIL
    assert result.reasons == [Reason.FAIL_TO_PASS_FAILING]
    assert result.fail_to_pass == {ABOVE: "failed", HIDDEN: "failed"}
    assert result.pass_to_pass == {INSIDE: "passed", BELOW: "passed"}
    assert result.patch_applied is None and result.patch_bytes == 0
    assert result.hidden_tests_applied is True
    assert result.test_counts == {"passed": 2, "failed": 2, "error": 0, "skipped": 0}
    assert {"build", "solve", "evaluate", "total"} <= set(result.durations)

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["counts"] == {"ERROR": 0, "FAIL": 1, "PASS": 0}
    assert summary["deterministic"] is None
    log = (run_dir / "logs" / "fixture_001.log").read_text(encoding="utf-8")
    assert (
        "verdict: FAIL" in log and "(none)" in log and "XX tests/test_clamp.py::test_above" in log
    )


def test_gold_oracle_passes_and_is_deterministic(bench: Path) -> None:
    code, run_dir = cli(
        bench, "results-gold", "--solver", "gold", "--expect", "pass", "--repeat", "2"
    )
    assert code == 0

    results = runner.load_results(run_dir / "results.jsonl")
    assert [r.repeat for r in results] == [0, 1]
    for result in results:
        assert result.status is Status.PASS and result.reasons == []
        assert result.patch_applied is True and result.hidden_tests_applied is True
        assert set(result.fail_to_pass.values()) == {"passed"}
        assert set(result.pass_to_pass.values()) == {"passed"}
        assert result.test_counts["passed"] == 4
    assert results[0].signature() == results[1].signature()

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["counts"] == {"ERROR": 0, "FAIL": 0, "PASS": 2}
    assert summary["deterministic"] is True and summary["pass_rate"] == 1.0
    assert (run_dir / "logs" / "fixture_001.0.log").exists()
    assert (run_dir / "logs" / "fixture_001.1.log").exists()


def test_expectation_violation_exits_nonzero(bench: Path) -> None:
    code, _ = cli(bench, "results-null-expect-pass", "--solver", "null", "--expect", "pass")
    assert code == 1


def test_wrong_patch_fails_with_regression(task: Task, image: str) -> None:
    class WrongSolver:
        name = "wrong"

        def solve(self, task: Task, image: str) -> str | None:
            return WRONG_PATCH

    result, log = run_task(task, WrongSolver())
    assert result.status is Status.FAIL
    assert result.reasons == [Reason.FAIL_TO_PASS_FAILING, Reason.PASS_TO_PASS_REGRESSED]
    assert result.fail_to_pass == {ABOVE: "failed", HIDDEN: "passed"}
    assert result.pass_to_pass == {INSIDE: "passed", BELOW: "failed"}
    assert result.patch_applied is True
    assert "return value" in log


def test_unappliable_patch_is_reported_not_raised(task: Task, image: str) -> None:
    class BrokenSolver:
        name = "broken"

        def solve(self, task: Task, image: str) -> str | None:
            return UNAPPLIABLE_PATCH

    result, _ = run_task(task, BrokenSolver())
    assert result.status is Status.FAIL
    assert result.reasons == [Reason.PATCH_APPLY_FAILED]
    assert result.patch_applied is False
    assert result.detail and "test_missing.py" in result.detail
    assert result.fail_to_pass == {} and result.test_counts == {}


def test_crashing_solver_is_an_error_not_a_crash(task: Task, image: str) -> None:
    class CrashingSolver:
        name = "crash"

        def solve(self, task: Task, image: str) -> str | None:
            raise RuntimeError("agent exploded")

    result, log = run_task(task, CrashingSolver())
    assert result.status is Status.ERROR
    assert result.reasons == [Reason.SOLVER_CRASHED]
    assert "agent exploded" in (result.detail or "") and "solver CRASHED" in log


def test_broken_hidden_tests_are_a_packaging_error(task: Task, image: str) -> None:
    broken = task.model_copy(update={"hidden_test_patch": UNAPPLIABLE_PATCH})

    untouched = evaluate_patch(broken, image, None)
    assert untouched.status is Status.ERROR
    assert untouched.reasons == (Reason.HIDDEN_TESTS_BROKEN,)

    with_candidate = evaluate_patch(broken, image, GOLD_PATCH)
    assert with_candidate.status is Status.FAIL
    assert with_candidate.reasons == (Reason.HIDDEN_TESTS_CONFLICT,)
    assert with_candidate.patch_applied is True and with_candidate.hidden_tests_applied is False
