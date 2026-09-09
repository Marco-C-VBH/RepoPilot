"""Runner pieces that need no Docker: result records, summaries, CLI plumbing."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from evals import runner
from evals.harness import TaskResult
from evals.judge import Reason, Status

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "evals" / "benchmark" / "examples"


def make_result(
    task_id: str,
    status: Status,
    *,
    repeat: int = 0,
    reasons: list[Reason] | None = None,
    f2p: dict[str, str | None] | None = None,
) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        solver="gold",
        repeat=repeat,
        status=status,
        reasons=reasons or [],
        fail_to_pass=f2p or {},
        started_at="2026-09-09T00:00:00+00:00",
        durations={"total": 1.5},
        test_counts={"passed": 2, "failed": 0, "error": 0, "skipped": 0},
    )


def test_task_result_round_trips_through_json() -> None:
    result = make_result("a_001", Status.FAIL, reasons=[Reason.FAIL_TO_PASS_FAILING])
    again = TaskResult.model_validate(json.loads(result.model_dump_json()))
    assert again == result
    assert again.reason == "fail_to_pass_failing"
    assert make_result("a_001", Status.PASS).reason == "-"


def test_summary_detects_nondeterministic_tasks(tmp_path: Path) -> None:
    results = [
        make_result("a_001", Status.PASS, repeat=0),
        make_result("b_002", Status.PASS, repeat=0, f2p={"t::x": "passed"}),
        make_result("a_001", Status.PASS, repeat=1),
        make_result("b_002", Status.FAIL, repeat=1, f2p={"t::x": "failed"}),
    ]
    summary = runner._summarize(results, "run", "gold", 2, 2, "start", time.monotonic(), tmp_path)
    assert summary.counts == {"ERROR": 0, "FAIL": 1, "PASS": 3}
    assert summary.pass_rate == 0.75
    assert summary.deterministic is False
    assert summary.nondeterministic_tasks == ["b_002"]


def test_summary_single_repeat_has_no_determinism_verdict(tmp_path: Path) -> None:
    results = [make_result("a_001", Status.ERROR, reasons=[Reason.IMAGE_BUILD_FAILED])]
    summary = runner._summarize(results, "run", "null", 1, 1, "start", time.monotonic(), tmp_path)
    assert summary.deterministic is None
    assert summary.counts["ERROR"] == 1
    assert summary.pass_rate == 0.0
    assert summary.limits["network"] == "none"


def test_format_row_is_compact() -> None:
    row = runner._format_row(make_result("a_001", Status.PASS))
    assert row.startswith("a_001")
    assert "PASS" in row and "2P 0F 0E 0S" in row and row.rstrip().endswith("1.5s")


def test_list_does_not_need_docker(capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.main(["--tasks", str(EXAMPLES), "--list"]) == 0
    out = capsys.readouterr().out
    assert "example_000" in out and "1 task(s) selected" in out


def test_bad_arguments_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.main(["--tasks", str(tmp_path / "missing")]) == 2
    assert "tasks directory not found" in capsys.readouterr().err
    assert runner.main(["--tasks", str(EXAMPLES), "--repeat", "0"]) == 2
    (tmp_path / "empty").mkdir()
    assert runner.main(["--tasks", str(tmp_path / "empty")]) == 2


def test_load_results_reads_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    rows = [make_result("a_001", Status.PASS), make_result("b_002", Status.FAIL)]
    path.write_text("".join(r.model_dump_json() + "\n" for r in rows), encoding="utf-8")
    assert runner.load_results(path) == rows
