"""The tool surface against a real sandbox (``-m docker``).

Workspace on the host, tests in the container: an edit made through
``edit_file`` must be what ``run_tests`` executes, and the workspace diff must
be judged the same way the harness judges a candidate patch.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from evals.benchmark.registry import load_tasks
from evals.benchmark.schema import Task
from evals.harness import evaluate_patch, image_spec_for
from evals.judge import Status
from repopilot.sandbox.docker import Sandbox, build_task_image, remove_image
from repopilot.tools import Toolbox, Workspace
from tests.fixture_repo import create_fixture_repo, fixture_task_dict

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("tools-bench")
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


def test_edit_then_run_tests_then_judge(bench: Path, task: Task, image: str) -> None:
    with (
        Workspace.create(
            task.repo, task.base_commit, task.bug_patch, cache_dir=bench / "cache"
        ) as ws,
        Sandbox(image) as sandbox,
    ):
        box = Toolbox(ws, sandbox, test_command=task.test_command, test_timeout=120)

        # The buggy tree fails its own suite exactly as the task says.
        before = box.call("run_tests", {})
        assert not before.is_error, before.output
        assert before.meta["counts"]["failed"] == 1
        assert before.meta["failed"] == ["tests/test_clamp.py::test_above"]
        assert before.meta["patch_applied"] is False

        # Look around with the read-only tools, then fix the off-by-one.
        assert "high - 1" in box.call("search_code", {"query": "high - 1"}).output
        assert box.call("search_symbol", {"name": "clamp"}).output.startswith(
            "fixturepkg/__init__.py:1-"
        )
        edited = box.call(
            "edit_file",
            {
                "path": "fixturepkg/__init__.py",
                "old_string": "min(value, high - 1)",
                "new_string": "min(value, high)",
            },
        )
        assert not edited.is_error, edited.output

        # A narrower target is allowed; the edit travels to the container as a diff.
        after = box.call("run_tests", {"target": "tests/test_clamp.py::test_above"})
        assert not after.is_error, after.output
        assert after.meta["counts"] == {"passed": 1, "failed": 0, "error": 0, "skipped": 0}
        assert after.meta["patch_applied"] is True
        assert box.test_runs == 2

        # The workspace diff is the candidate patch, and the harness passes it.
        patch = ws.diff()
        assert "+    return max(low, min(value, high))" in patch
        verdict = evaluate_patch(task, image, patch)
        assert verdict.status is Status.PASS, verdict


def test_syntax_error_reaches_the_agent_as_a_missing_report(
    bench: Path, task: Task, image: str
) -> None:
    with (
        Workspace.create(
            task.repo, task.base_commit, task.bug_patch, cache_dir=bench / "cache"
        ) as ws,
        Sandbox(image) as sandbox,
    ):
        box = Toolbox(ws, sandbox, test_command=task.test_command, test_timeout=120)
        box.call(
            "edit_file",
            {
                "path": "fixturepkg/__init__.py",
                "old_string": "def clamp(",
                "new_string": "def clamp(",
            },
        )  # identical -> refused, tree unchanged
        broken = box.call(
            "edit_file",
            {
                "path": "fixturepkg/__init__.py",
                "old_string": "def clamp(value, low, high):",
                "new_string": "def clamp(value, low, high)",
            },
        )
        assert not broken.is_error
        result = box.call("run_tests", {})
        # Either pytest could not collect (report with collection errors) or it died
        # before writing a report; both must reach the model as a visible problem.
        assert "collection errors" in result.output or "no report" in result.output
        assert result.meta["counts"]["passed"] == 0
