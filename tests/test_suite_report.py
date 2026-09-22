"""The suite report (evals/benchmark/suite.py): counts and the v1 structural targets."""

from __future__ import annotations

from pathlib import Path

from evals.benchmark.schema import Task
from evals.benchmark.suite import V1_TARGETS, format_suite_report, suite_report
from tests.fixture_repo import fixture_task_dict


def _task(repo: Path, sha: str, task_id: str, **overrides: object) -> Task:
    data = fixture_task_dict(repo, sha, task_id=task_id)
    data.update(overrides)
    return Task.model_validate(data)


def test_suite_report_counts_and_checks_targets(tmp_path: Path) -> None:
    repo, sha = tmp_path / "repo", "a" * 40
    v0 = _task(repo, sha, "fixture_001")
    v1 = _task(
        repo,
        sha,
        "fixture_002",
        suite="v1",
        report_level="symptom_only",
        entry_points=["clamp"],
        surface_symbols=["clamp"],
        surface_files=["fixturepkg/__init__.py"],
        cross_module=False,
        difficulty="medium",
    )
    report = suite_report([v0, v1])
    assert report["all"]["tasks"] == 2 and report["v0"]["tasks"] == 1
    new = report["v1_new"]
    assert new["tasks"] == 1 and new["symptom_only"] == 1 and new["cross_module"] == 0
    assert new["difficulty"] == {"medium": 1}
    targets = report["targets"]
    assert targets["tasks"] == {"actual": 1, "target": V1_TARGETS["tasks"], "met": False}
    assert targets["changed_lines"]["met"] is True
    assert "off_by_one" in report["thin_categories"]
    assert report["unaudited"] == ["fixture_001"]  # the v0 file has no surface yet
    assert report["difficulty_mismatch"] == []  # easy is what the rule gives an unaudited task
    text = format_suite_report(report)
    assert "v1-new    1 task(s)" in text and "MISSED tasks" in text
    assert "unaudited (run make_task --refresh): fixture_001" in text


def test_suite_report_without_v1_tasks_prints_no_targets(tmp_path: Path) -> None:
    report = suite_report([_task(tmp_path / "repo", "a" * 40, "fixture_001")])
    assert report["v1_new"]["tasks"] == 0
    assert "targets" not in format_suite_report(report)
