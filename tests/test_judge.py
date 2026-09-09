"""Verdict logic (evals/judge.py) and the Phase 0 oracle solvers (evals/solvers.py)."""

from __future__ import annotations

import json

import pytest

from evals.benchmark.schema import Task
from evals.judge import Reason, Status, judge
from evals.solvers import GoldSolver, NullSolver, get_solver
from repopilot.sandbox.results import TestRun, parse_report
from tests.test_schema import make_task

F2P = ["t.py::fixed_a", "t.py::fixed_b"]
P2P = ["t.py::old_a", "t.py::old_b"]


def make_run(outcomes: dict[str, str], *, timed_out: bool = False, report: bool = True) -> TestRun:
    parsed = parse_report(
        json.dumps({"tests": {nodeid: {"outcome": o} for nodeid, o in outcomes.items()}})
    )
    return TestRun(
        command="pytest",
        exit_code=0 if all(o == "passed" for o in outcomes.values()) else 1,
        timed_out=timed_out,
        duration_seconds=1.0,
        stdout="",
        stderr="",
        tests=parsed.tests if report else {},
        report_found=report,
    )


def test_pass_requires_every_listed_test_to_pass() -> None:
    run = make_run({**dict.fromkeys(F2P, "passed"), **dict.fromkeys(P2P, "passed")})
    verdict = judge(run, F2P, P2P)
    assert verdict.status is Status.PASS
    assert verdict.reasons == ()
    assert verdict.failing_fail_to_pass == []
    assert verdict.regressed_pass_to_pass == []


def test_failing_fail_to_pass_test_fails_the_task() -> None:
    run = make_run({F2P[0]: "passed", F2P[1]: "failed", **dict.fromkeys(P2P, "passed")})
    verdict = judge(run, F2P, P2P)
    assert verdict.status is Status.FAIL
    assert verdict.reasons == (Reason.FAIL_TO_PASS_FAILING,)
    assert verdict.failing_fail_to_pass == [F2P[1]]
    assert verdict.fail_to_pass == {F2P[0]: "passed", F2P[1]: "failed"}


def test_unreported_test_counts_as_not_passed() -> None:
    run = make_run({F2P[0]: "passed", **dict.fromkeys(P2P, "passed")})  # F2P[1] never ran
    verdict = judge(run, F2P, P2P)
    assert verdict.status is Status.FAIL
    assert verdict.fail_to_pass[F2P[1]] is None
    assert verdict.failing_fail_to_pass == [F2P[1]]


@pytest.mark.parametrize("bad", ["failed", "error", "skipped"])
def test_regression_in_pass_to_pass_fails_the_task(bad: str) -> None:
    run = make_run({**dict.fromkeys(F2P, "passed"), P2P[0]: "passed", P2P[1]: bad})
    verdict = judge(run, F2P, P2P)
    assert verdict.status is Status.FAIL
    assert verdict.reasons == (Reason.PASS_TO_PASS_REGRESSED,)
    assert verdict.regressed_pass_to_pass == [P2P[1]]


def test_timeout_without_report_lists_every_reason() -> None:
    run = make_run({}, timed_out=True, report=False)
    verdict = judge(run, F2P, P2P)
    assert verdict.status is Status.FAIL
    assert verdict.reasons == (
        Reason.TESTS_TIMED_OUT,
        Reason.NO_TEST_REPORT,
        Reason.FAIL_TO_PASS_FAILING,
        Reason.PASS_TO_PASS_REGRESSED,
    )
    assert set(verdict.fail_to_pass.values()) == {None}


def test_empty_pass_to_pass_is_allowed() -> None:
    run = make_run(dict.fromkeys(F2P, "passed"))
    assert judge(run, F2P, []).status is Status.PASS


def test_extra_tests_in_the_run_do_not_matter() -> None:
    run = make_run({**dict.fromkeys(F2P, "passed"), "t.py::unrelated": "failed"})
    assert judge(run, F2P, []).status is Status.PASS


# -- solvers ------------------------------------------------------------------


def test_null_and_gold_solvers() -> None:
    task = Task.model_validate(make_task())
    assert NullSolver().solve(task, "img") is None
    assert GoldSolver().solve(task, "img") == task.gold_patch
    assert get_solver("null").name == "null"
    assert get_solver("gold").name == "gold"


def test_unknown_solver_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown solver 'agent'"):
        get_solver("agent")
