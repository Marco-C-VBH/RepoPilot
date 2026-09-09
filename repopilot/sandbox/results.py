"""Structured results of commands and test runs executed inside a sandbox."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum


class TestOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


TestOutcome.__test__ = False  # type: ignore[attr-defined]  # not a pytest test class


@dataclass(frozen=True)
class TestResult:
    """Outcome of one pytest node id (exact, as pytest reports it)."""

    __test__ = False  # keep pytest from collecting this as a test class

    nodeid: str
    outcome: TestOutcome
    duration: float = 0.0
    message: str | None = None
    longrepr: str | None = None


@dataclass(frozen=True)
class ExecResult:
    """A command executed inside a sandbox container."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class ParsedReport:
    exit_status: int | None
    tests: dict[str, TestResult]
    collection_errors: tuple[str, ...] = ()


def parse_report(text: str) -> ParsedReport:
    """Parse the JSON written by ``repopilot_pytest_plugin`` inside the container."""
    data = json.loads(text)
    tests: dict[str, TestResult] = {}
    for nodeid, entry in (data.get("tests") or {}).items():
        try:
            outcome = TestOutcome(entry.get("outcome"))
        except ValueError:
            outcome = TestOutcome.ERROR
        tests[nodeid] = TestResult(
            nodeid=nodeid,
            outcome=outcome,
            duration=float(entry.get("duration") or 0.0),
            message=entry.get("message"),
            longrepr=entry.get("longrepr"),
        )
    exit_status = data.get("exit_status")
    return ParsedReport(
        exit_status=int(exit_status) if exit_status is not None else None,
        tests=tests,
        collection_errors=tuple(data.get("collection_errors") or ()),
    )


@dataclass(frozen=True)
class TestRun:
    """One ``Sandbox.run_tests`` invocation: process outcome plus per-test outcomes.

    ``tests`` is empty and ``report_found`` is False when pytest never reached
    session end (timeout, crash, wrong command) -- callers must treat a missing
    node id as *not passed*, never as passed.
    """

    __test__ = False  # keep pytest from collecting this as a test class

    command: str
    exit_code: int
    timed_out: bool
    duration_seconds: float
    stdout: str
    stderr: str
    tests: dict[str, TestResult] = field(default_factory=dict)
    report_found: bool = False
    pytest_exit_status: int | None = None
    collection_errors: tuple[str, ...] = ()

    def outcome(self, nodeid: str) -> TestOutcome | None:
        result = self.tests.get(nodeid)
        return result.outcome if result is not None else None

    def passed(self, nodeid: str) -> bool:
        return self.outcome(nodeid) is TestOutcome.PASSED

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(TestOutcome, 0)
        for result in self.tests.values():
            counts[result.outcome] += 1
        return {str(k): v for k, v in counts.items()}

    def summary(self) -> str:
        c = self.counts()
        parts = [
            f"{c['passed']} passed, {c['failed']} failed, {c['error']} errors, "
            f"{c['skipped']} skipped",
            f"exit {self.exit_code}",
            f"{self.duration_seconds:.1f}s",
        ]
        if self.timed_out:
            parts.append("TIMED OUT")
        if not self.report_found:
            parts.append("no report")
        return " · ".join(parts)
