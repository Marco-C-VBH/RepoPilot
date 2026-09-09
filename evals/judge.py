"""Deterministic task verdict (spec §10.4).

A task PASSes only when every ``fail_to_pass`` test passed *and* every
``pass_to_pass`` test still passed in the evaluation run.  Anything else --
a failing test, a test that never reported (timeout, crash, collection error,
renamed test), a regression -- is a FAIL with one or more reason codes.  No LLM
judge anywhere.

Reason codes are deliberately coarse and stable: they are the first level of
the failure taxonomy (spec §11.2) and get aggregated across runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from repopilot.sandbox.results import TestOutcome, TestRun


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"  # the harness, not the candidate, is at fault (image build, packaging)


class Reason(StrEnum):
    # verdicts on a candidate patch
    TESTS_TIMED_OUT = "tests_timed_out"
    NO_TEST_REPORT = "no_test_report"
    FAIL_TO_PASS_FAILING = "fail_to_pass_failing"
    PASS_TO_PASS_REGRESSED = "pass_to_pass_regressed"
    PATCH_APPLY_FAILED = "patch_apply_failed"
    HIDDEN_TESTS_CONFLICT = "hidden_tests_conflict"
    # harness / environment problems (Status.ERROR)
    HIDDEN_TESTS_BROKEN = "hidden_tests_broken"
    IMAGE_BUILD_FAILED = "image_build_failed"
    SOLVER_CRASHED = "solver_crashed"
    SANDBOX_ERROR = "sandbox_error"


@dataclass(frozen=True)
class Verdict:
    status: Status
    reasons: tuple[Reason, ...]
    fail_to_pass: dict[str, str | None]  # node id -> outcome, None when not reported
    pass_to_pass: dict[str, str | None]

    @property
    def failing_fail_to_pass(self) -> list[str]:
        return [t for t, outcome in self.fail_to_pass.items() if outcome != TestOutcome.PASSED]

    @property
    def regressed_pass_to_pass(self) -> list[str]:
        return [t for t, outcome in self.pass_to_pass.items() if outcome != TestOutcome.PASSED]


def _outcomes(run: TestRun, node_ids: Sequence[str]) -> dict[str, str | None]:
    return {
        node_id: (str(outcome) if (outcome := run.outcome(node_id)) is not None else None)
        for node_id in node_ids
    }


def judge(run: TestRun, fail_to_pass: Sequence[str], pass_to_pass: Sequence[str]) -> Verdict:
    """Combine a test run with the task's success criteria into a Verdict."""
    f2p = _outcomes(run, fail_to_pass)
    p2p = _outcomes(run, pass_to_pass)

    reasons: list[Reason] = []
    if run.timed_out:
        reasons.append(Reason.TESTS_TIMED_OUT)
    if not run.report_found:
        reasons.append(Reason.NO_TEST_REPORT)
    if any(outcome != TestOutcome.PASSED for outcome in f2p.values()):
        reasons.append(Reason.FAIL_TO_PASS_FAILING)
    if any(outcome != TestOutcome.PASSED for outcome in p2p.values()):
        reasons.append(Reason.PASS_TO_PASS_REGRESSED)

    status = Status.PASS if not reasons else Status.FAIL
    return Verdict(status=status, reasons=tuple(reasons), fail_to_pass=f2p, pass_to_pass=p2p)
