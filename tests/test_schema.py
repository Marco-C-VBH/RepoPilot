"""Unit tests for the task schema (evals/benchmark/schema.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evals.benchmark.schema import (
    Task,
    TaskSource,
    looks_like_unified_diff,
    task_json_schema,
    touched_files,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_TASK = REPO_ROOT / "evals" / "benchmark" / "examples" / "example_000.json"
SCHEMA_FILE = REPO_ROOT / "evals" / "benchmark" / "task.schema.json"

SHA = "a" * 40

GOLD_PATCH = """\
diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,2 +1,3 @@
 def f(items):
+    assert items
     return items[0]
"""

PLAIN_PATCH = """\
--- src/x.py
+++ src/x.py
@@ -1,2 +1,3 @@
 def f(items):
+    assert items
     return items[0]
"""


def make_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": "demo_001",
        "repo": "https://github.com/example/demo",
        "base_commit": SHA,
        "source": "real",
        "description": "f() crashes on an empty list.",
        "gold_patch": GOLD_PATCH,
        "fail_to_pass": ["tests/test_x.py::test_empty"],
        "pass_to_pass": ["tests/test_x.py::test_basic"],
        "test_command": "pytest tests/test_x.py",
        "gold_files": ["src/x.py"],
        "gold_symbols": ["f"],
        "category": "missing_check",
        "difficulty": "easy",
    }
    task.update(overrides)
    return task


def test_example_task_validates() -> None:
    task = Task.model_validate(json.loads(EXAMPLE_TASK.read_text(encoding="utf-8")))
    assert task.id == "example_000"
    assert task.is_mutation
    assert task.gold_patch_files == {"src/payment/retry.py"}
    assert task.all_tests[0] == task.fail_to_pass[0]


def test_minimal_real_task_defaults() -> None:
    task = Task.model_validate(make_task())
    assert task.source is TaskSource.REAL
    assert task.bug_patch is None
    assert task.hidden_test_patch is None
    assert task.env.python == "3.11"
    assert task.env.install == "pip install -e ."
    assert task.env.test_timeout_seconds == 300


def test_round_trips_through_json() -> None:
    task = Task.model_validate(make_task())
    again = Task.model_validate_json(task.model_dump_json())
    assert again == task


def test_task_is_frozen() -> None:
    task = Task.model_validate(make_task())
    with pytest.raises(ValidationError):
        task.id = "other"  # type: ignore[misc]


def test_mutation_requires_bug_patch() -> None:
    with pytest.raises(ValidationError, match="mutation tasks require bug_patch"):
        Task.model_validate(make_task(source="mutation"))


def test_real_task_rejects_bug_patch() -> None:
    with pytest.raises(ValidationError, match="real tasks must not have bug_patch"):
        Task.model_validate(make_task(source="real", bug_patch=GOLD_PATCH))


def test_gold_files_must_be_touched_by_gold_patch() -> None:
    with pytest.raises(ValidationError, match=r"not modified by gold_patch: \['src/other.py'\]"):
        Task.model_validate(make_task(gold_files=["src/x.py", "src/other.py"]))


def test_fail_to_pass_and_pass_to_pass_must_be_disjoint() -> None:
    with pytest.raises(ValidationError, match="both fail_to_pass and pass_to_pass"):
        Task.model_validate(make_task(pass_to_pass=["tests/test_x.py::test_empty"]))


def test_fail_to_pass_cannot_be_empty() -> None:
    with pytest.raises(ValidationError, match="fail_to_pass"):
        Task.model_validate(make_task(fail_to_pass=[]))


def test_duplicate_test_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        Task.model_validate(make_task(pass_to_pass=["t::a", "t::a"]))


@pytest.mark.parametrize("patch", ["", "not a diff", "+++ b/src/x.py\n(no hunk)"])
def test_malformed_patches_rejected(patch: str) -> None:
    with pytest.raises(ValidationError, match="unified diff"):
        Task.model_validate(make_task(gold_patch=patch))


def test_plain_diff_u_format_is_accepted() -> None:
    task = Task.model_validate(make_task(gold_patch=PLAIN_PATCH))
    assert task.gold_patch_files == {"src/x.py"}


@pytest.mark.parametrize("commit", ["abc123", "A" * 40, "g" * 40, ""])
def test_short_or_invalid_commits_rejected(commit: str) -> None:
    with pytest.raises(ValidationError, match="base_commit"):
        Task.model_validate(make_task(base_commit=commit))


@pytest.mark.parametrize("task_id", ["Demo_001", "demo-001", "_demo", "demo__001", ""])
def test_invalid_ids_rejected(task_id: str) -> None:
    with pytest.raises(ValidationError, match="id"):
        Task.model_validate(make_task(id=task_id))


@pytest.mark.parametrize(
    "path",
    ["/src/x.py", "../src/x.py", "src/../x.py", "./src/x.py", "src//x.py", "src/", "src\\x.py"],
)
def test_gold_files_must_be_repo_relative(path: str) -> None:
    with pytest.raises(ValidationError, match="relative POSIX path"):
        Task.model_validate(make_task(gold_files=[path]))


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError, match="gold_file"):
        Task.model_validate(make_task(gold_file="src/x.py"))


@pytest.mark.parametrize("category", ["state_management", "off_by_one", "other"])
def test_known_categories_accepted(category: str) -> None:
    assert Task.model_validate(make_task(category=category)).category == category


def test_unknown_category_rejected() -> None:
    with pytest.raises(ValidationError, match="category"):
        Task.model_validate(make_task(category="typo"))


@pytest.mark.parametrize("version", ["3", "3.11.4", "2.7", "py311"])
def test_env_python_version_format(version: str) -> None:
    with pytest.raises(ValidationError, match="python"):
        Task.model_validate(make_task(env={"python": version}))


# -- helpers -------------------------------------------------------------


def test_touched_files_prefers_git_headers() -> None:
    patch = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/tests/test_a.py b/tests/test_a.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/tests/test_a.py\n@@ -0,0 +1 @@\n+def test(): ...\n"
    )
    assert touched_files(patch) == {"src/a.py", "tests/test_a.py"}


def test_touched_files_ignores_dev_null_in_plain_format() -> None:
    patch = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x = 1\n"
    assert touched_files(patch) == {"new.py"}


def test_touched_files_handles_reversed_and_prefixless_git_headers() -> None:
    reversed_prefixes = (
        "diff --git b/src/a.py a/src/a.py\n--- b/src/a.py\n+++ a/src/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    assert touched_files(reversed_prefixes) == {"src/a.py"}
    no_prefix = "diff --git src/a.py src/a.py\n--- src/a.py\n+++ src/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    assert touched_files(no_prefix) == {"src/a.py"}
    plain_reversed = "--- b/src/a.py\n+++ a/src/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    assert touched_files(plain_reversed) == {"src/a.py"}


def test_touched_files_ignores_removed_lines_that_look_like_headers() -> None:
    patch = (
        "diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n"
        "@@ -1 +1 @@\n--- old comment\n+-- new comment\n"
    )
    assert touched_files(patch) == {"q.sql"}


def test_looks_like_unified_diff() -> None:
    assert looks_like_unified_diff(GOLD_PATCH)
    assert looks_like_unified_diff(PLAIN_PATCH)
    assert not looks_like_unified_diff("@@ -1 +1 @@\n-x\n+y\n")  # hunk without file header
    assert not looks_like_unified_diff("+++ b/x.py\n")  # header without hunk


def test_exported_json_schema_is_current() -> None:
    expected = json.dumps(task_json_schema(), indent=2) + "\n"
    assert SCHEMA_FILE.read_text(encoding="utf-8") == expected, (
        "task.schema.json is stale; run: uv run python scripts/export_task_schema.py"
    )
