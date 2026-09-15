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


def test_budget_flags_override_defaults() -> None:
    args = runner.build_parser().parse_args(
        ["--solver", "baseline", "--max-steps", "5", "--max-cost", "0.1", "--max-run-cost", "3"]
    )
    budget = runner.budget_from_args(args)
    assert budget.max_steps == 5 and budget.max_cost_usd == 0.1
    assert budget.max_tool_calls == 40 and budget.max_runtime_seconds == 600  # untouched defaults
    assert args.max_run_cost == 3.0


def test_build_solver_checks_keys_for_the_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evals.solvers import BaselineSolver, NullSolver
    from repopilot.models.config import ConfigError

    parser = runner.build_parser()
    assert isinstance(runner.build_solver(parser.parse_args(["--solver", "null"])), NullSolver)

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    args = parser.parse_args(
        ["--solver", "baseline", "--model", "gpt-5.6-luna", "--env-file", str(tmp_path / "none")]
    )
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        runner.build_solver(args)

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    args = parser.parse_args(
        ["--solver", "baseline", "--model", "gpt-5.6-luna", "--max-steps", "7", "--env-file", "x"]
    )
    solver = runner.build_solver(args)
    assert isinstance(solver, BaselineSolver)
    assert solver.model == "gpt-5.6-luna" and solver.budget.max_steps == 7


def test_archive_run_copies_summary_results_and_traces(tmp_path: Path) -> None:
    from scripts.archive_run import archive

    run_dir = tmp_path / "results" / "baseline-x"
    (run_dir / "traces").mkdir(parents=True)
    (run_dir / "logs").mkdir()
    (run_dir / "summary.json").write_text(
        json.dumps({"solver": "baseline", "tasks": 1, "repeat": 1, "pass_rate": 1.0}),
        encoding="utf-8",
    )
    (run_dir / "results.jsonl").write_text(
        make_result("a_001", Status.PASS).model_dump_json() + "\n"
    )
    (run_dir / "traces" / "a_001.jsonl").write_text('{"seq": 0, "kind": "run_start"}\n')
    (run_dir / "logs" / "a_001.log").write_text("noise\n")

    target = archive(run_dir, "demo", out_root=tmp_path / "experiments")
    assert sorted(p.name for p in target.iterdir()) == ["results.jsonl", "summary.json", "traces"]
    assert (target / "traces" / "a_001.jsonl").read_text().startswith('{"seq": 0')
    assert json.loads((target / "summary.json").read_text())["archived_from"] == str(run_dir)
    with pytest.raises(SystemExit, match="exists"):
        archive(run_dir, "demo", out_root=tmp_path / "experiments")
    archive(run_dir, "demo", out_root=tmp_path / "experiments", force=True)


def test_leak_scan_flags_hidden_markers_and_reports_localization(tmp_path: Path) -> None:
    from scripts.leak_scan import scan

    tasks = {
        "a_001": {
            "id": "a_001",
            "gold_files": ["pkg/core.py"],
            "hidden_test_patch": (
                "diff --git a/tests/test_hidden.py b/tests/test_hidden.py\n"
                "--- /dev/null\n+++ b/tests/test_hidden.py\n@@ -0,0 +1,2 @@\n"
                "+def test_secret_behaviour():\n+    assert True\n"
            ),
        }
    }

    def event(seq: int, **data: object) -> str:
        return json.dumps({"seq": seq, "t": 0.0, **data}) + "\n"

    clean = tmp_path / "results" / "run-clean" / "traces"
    clean.mkdir(parents=True)
    (clean / "a_001.jsonl").write_text(
        event(0, kind="run_start", model="m1")
        + event(1, kind="tool_call", name="run_tests", arguments={}, output="1 failed")
        + event(2, kind="tool_call", name="read_file", arguments={"path": "pkg/core.py"}, output="")
        + event(3, kind="tool_call", name="edit_file", arguments={"path": "pkg/core.py"}, output="")
    )
    findings, by_model = scan([tmp_path / "results"], tasks)
    assert findings == []
    assert by_model["m1"]["first call: run_tests"] == 1
    assert by_model["m1"]["first edit in a gold file"] == 1
    assert by_model["m1"]["ran tests before the first edit"] == 1

    # An archived copy of the same run is counted once, not twice.
    archived = tmp_path / "experiments" / "demo"
    shutil.copytree(clean.parent, archived)
    (archived / "summary.json").write_text(json.dumps({"archived_from": "results/run-clean"}))
    _, by_model = scan([tmp_path / "results", tmp_path / "experiments"], tasks)
    assert by_model["m1"]["runs"] == 1

    leaky = tmp_path / "results" / "run-leaky" / "traces"
    leaky.mkdir(parents=True)
    (leaky / "a_001.jsonl").write_text(
        event(0, kind="run_start", model="m1")
        + event(
            1,
            kind="tool_call",
            name="run_tests",
            arguments={},
            output="test_secret_behaviour FAILED",
        )
        + event(
            2, kind="tool_call", name="read_file", arguments={"path": "../outside.py"}, output=""
        )
    )
    findings, _ = scan([tmp_path / "results"], tasks)
    assert len(findings) == 2
    assert "test_secret_behaviour" in findings[0] and "outside the repository" in findings[1]
