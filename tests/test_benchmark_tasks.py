"""Integrity checks over the shipped benchmark (evals/benchmark/tasks + sources).

These run against the real task files, not fixtures, so a hand edit to a task
JSON, a source directory that drifted from its generated task, or a task
committed without its audit trail fails the unit suite (and CI) before it can
skew a benchmark run.  Nothing here needs Docker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.benchmark.authoring import SOURCES_DIR, is_test_path, load_draft
from evals.benchmark.registry import TASKS_DIR, load_tasks
from evals.benchmark.schema import ReportLevel, Suite, Task, TaskSource, touched_files

TASKS = load_tasks(TASKS_DIR)
IDS = [t.id for t in TASKS]


def test_benchmark_is_not_empty() -> None:
    assert len(TASKS) >= 10, "Phase 0 needs 10-20 tasks"


@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_id_names_the_repository(task: Task) -> None:
    repo_name = task.repo.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    prefix, _, number = task.id.rpartition("_")
    assert prefix == repo_name, f"{task.id} should be named after {repo_name}"
    assert number.isdigit() and len(number) == 3


@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_task_matches_its_source_directory(task: Task) -> None:
    source = SOURCES_DIR / task.id
    assert source.is_dir(), f"{task.id} has no source directory under {SOURCES_DIR}"
    draft = load_draft(source)
    assert draft.id == task.id
    assert draft.repo == task.repo
    assert draft.commit == task.base_commit
    assert draft.category == task.category
    assert draft.suite == task.suite
    assert draft.report_level == task.report_level
    assert list(draft.entry_points) == task.entry_points
    assert draft.source == task.source
    assert draft.fix_commit == task.fix_commit
    assert draft.test_command == task.test_command
    assert draft.env == task.env
    assert draft.description == task.description
    if task.source is TaskSource.MUTATION:
        assert draft.bug_patch is not None
        assert touched_files(task.bug_patch) == touched_files(draft.bug_patch)
    else:
        assert draft.bug_patch is None and task.bug_patch is None
    # make_task re-exports both patches through git, so their text can differ from
    # the source files in index lines; the set of files they touch cannot.
    assert draft.hidden_patch is not None
    assert touched_files(task.hidden_test_patch) == touched_files(draft.hidden_patch)


@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_task_has_been_audited_and_its_difficulty_is_derived(task: Task) -> None:
    """docs/bench-v1-design.md §7: difficulty is a rule, not a judgement."""
    assert task.audited, f"{task.id}: run make_task --refresh to fill the audit fields"
    assert task.difficulty == task.derived_difficulty
    if task.report_level is ReportLevel.SYMPTOM_ONLY:
        assert task.entry_points
    if task.suite is Suite.V1:
        assert task.report_level is not ReportLevel.INTERNAL, "v1 reports name no internals"


@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_mutation_and_fix_touch_the_same_source_files(task: Task) -> None:
    fixed = touched_files(task.gold_patch)
    if task.bug_patch is not None:
        assert touched_files(task.bug_patch) == fixed
    assert not any(is_test_path(p) for p in fixed), "fixes must not touch tests"
    assert set(task.gold_files) <= fixed
    assert task.gold_symbols, "make_task should have derived at least one symbol"


@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_hidden_tests_are_new_files_that_catch_the_bug(task: Task) -> None:
    hidden = task.hidden_test_files
    assert hidden, "every Phase 0 task ships a hidden regression test"
    for path in hidden:
        assert is_test_path(path)
        assert Path(path).name == f"test_repopilot_{task.id}.py"
        assert f"+++ b/{path}" in task.hidden_test_patch
        assert f"--- a/{path}" not in task.hidden_test_patch, "hidden patch must add, not edit"
        hidden_ids = [t for t in task.fail_to_pass if t.startswith(path + "::")]
        assert hidden_ids, f"{path} contributes nothing to fail_to_pass"
    # A hidden test that passes with the bug is a regression guard transplanted with a
    # real fix, and the task must say so; without the flag it is an authoring accident.
    assert bool(task.hidden_guards) == task.hidden_pass_to_pass, (
        f"hidden tests in pass_to_pass: {task.hidden_guards}; "
        f"hidden_pass_to_pass={task.hidden_pass_to_pass}"
    )
    assert task.pass_to_pass, "an empty pass_to_pass would let a fix delete the suite"


@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_test_command_targets_only_existing_visible_files(task: Task) -> None:
    words = task.test_command.split()
    assert words[0] == "pytest"
    targets = [w for w in words[1:] if not w.startswith("-")]
    assert targets, "a bare `pytest` would collect the whole repository"
    hidden = set(task.hidden_test_files)
    for target in targets:
        assert target not in hidden, "hidden files are appended at evaluation time"
        assert target.endswith(".py"), f"target {target!r} should be a test file"
        assert "test_tornado" not in target, "tenacity's tornado tests need tornado"
