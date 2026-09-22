"""Solver pieces that need no Docker or model: the report switch of the leak ablation."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.benchmark.audit import GENERIC_REPORT
from evals.benchmark.schema import Task
from evals.solvers import BaselineSolver, StructuredSolver, get_solver
from repopilot.tools.workspace import Workspace
from tests.fixture_repo import create_fixture_repo, fixture_task_dict


@pytest.fixture
def task_and_workspace(tmp_path: Path) -> tuple[Task, Workspace]:
    repo = tmp_path / "repo"
    sha = create_fixture_repo(repo)
    task = Task.model_validate(fixture_task_dict(repo, sha))
    workspace = Workspace.create(str(repo), sha, task.bug_patch, cache_dir=tmp_path / "cache")
    return task, workspace


def test_report_text_follows_the_switch(task_and_workspace: tuple[Task, Workspace]) -> None:
    task, workspace = task_and_workspace
    with workspace:
        assert BaselineSolver(report="full").report_text(task, workspace) == task.description
        assert BaselineSolver(report="generic").report_text(task, workspace) == GENERIC_REPORT
        redacted = BaselineSolver(report="redacted").report_text(task, workspace)
    assert "clamp" not in redacted
    assert redacted.startswith("a function returns a value one below the upper bound")
    assert "instead of 10" in redacted  # the rest of the report survives


def test_report_switch_is_validated_and_reaches_both_solvers() -> None:
    with pytest.raises(ValueError, match="report must be one of"):
        BaselineSolver(report="loud")
    assert get_solver("structured", report="generic").report == "generic"
    assert isinstance(get_solver("baseline", run_tests=False), BaselineSolver)
    assert get_solver("baseline", run_tests=False).run_tests is False
    assert isinstance(get_solver("structured"), StructuredSolver)
