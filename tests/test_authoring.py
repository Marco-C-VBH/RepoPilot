"""Task authoring without Docker: source loading, patch normalization, symbol lookup."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from evals.benchmark.authoring import (
    AuthoringError,
    changed_lines,
    enclosing_symbols,
    init_draft,
    is_test_path,
    load_draft,
    normalize,
)
from evals.benchmark.schema import Category, Difficulty
from tests.fixture_repo import BUG_PATCH, HIDDEN_TEST_PATCH, create_fixture_repo

REPO_ROOT = Path(__file__).resolve().parents[1]

TASK_TOML = """\
id = "{id}"
repo = "{repo}"
commit = "{commit}"
category = "off_by_one"
difficulty = "easy"
test_command = "pytest tests"

[env]
python = "3.11"
install = "true"
test_timeout_seconds = 120
"""


def write_source(
    directory: Path,
    repo: Path,
    sha: str,
    *,
    task_id: str = "fixture_001",
    bug_patch: str = BUG_PATCH,
    hidden_patch: str | None = HIDDEN_TEST_PATCH,
    toml: str | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "task.toml").write_text(
        toml if toml is not None else TASK_TOML.format(id=task_id, repo=repo, commit=sha),
        encoding="utf-8",
    )
    (directory / "bug.patch").write_text(bug_patch, encoding="utf-8")
    if hidden_patch is not None:
        (directory / "hidden.patch").write_text(hidden_patch, encoding="utf-8")
    (directory / "description.md").write_text(
        "clamp(15, 0, 10) returns 9 instead of 10.\n", encoding="utf-8"
    )
    return directory


@pytest.fixture
def fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    return repo, create_fixture_repo(repo)


# -- diff line bookkeeping + AST ---------------------------------------------------


def test_changed_lines_tracks_old_side_positions() -> None:
    patch = (
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n"
        "@@ -10,6 +10,7 @@ def f():\n"
        " a\n"  # old 10
        "-b\n"  # old 11 removed
        "+B\n"  # inserted before old 12
        " c\n"  # old 12
        "+extra\n"  # inserted before old 13
        " d\n"  # old 13
        "-e\n"  # old 14 removed
        " f\n"  # old 15
    )
    changed = changed_lines(patch)
    assert changed.modified == {"m.py": {11, 14}}
    assert changed.inserted_before == {"m.py": {12, 13}}


SOURCE = """\
import os


def helper(x):
    return x + 1


class Cache:
    def get(self, key):
        return self._data[key]

    @property
    def size(self):
        return len(self._data)

    class Inner:
        def deep(self):
            return 1


CONSTANT = 3
"""


def test_enclosing_symbols_are_qualified_and_innermost() -> None:
    assert enclosing_symbols(SOURCE, {5}, set()) == ["helper"]
    assert enclosing_symbols(SOURCE, {10}, set()) == ["Cache.get"]
    assert enclosing_symbols(SOURCE, {12}, set()) == ["Cache.size"], "decorator line belongs to it"
    assert enclosing_symbols(SOURCE, {18}, set()) == ["Cache.Inner.deep"]
    assert enclosing_symbols(SOURCE, {21}, set()) == [], "module-level change has no symbol"
    assert enclosing_symbols(SOURCE, {5, 10, 18}, set()) == [
        "helper",
        "Cache.get",
        "Cache.Inner.deep",
    ]


def test_insertion_at_end_of_function_is_attributed_to_that_function() -> None:
    # inserting before line 6 (the blank line after helper's body) means appending to helper
    assert enclosing_symbols(SOURCE, set(), {6}) == ["helper"]
    # inserting before line 4 (the def line) attaches to whatever line 3/4 is: helper starts at 4
    assert enclosing_symbols(SOURCE, set(), {4}) == ["helper"]


def test_is_test_path() -> None:
    assert is_test_path("tests/test_lru.py")
    assert is_test_path("toolz/tests/test_itertoolz.py")
    assert is_test_path("src/pkg/conftest.py")
    assert is_test_path("pkg/foo_test.py")
    assert not is_test_path("src/cachetools/__init__.py")
    assert not is_test_path("tenacity/retry.py")


# -- loading a source directory ----------------------------------------------------


def test_load_draft_reads_everything(tmp_path: Path, fixture_repo: tuple[Path, str]) -> None:
    repo, sha = fixture_repo
    draft = load_draft(write_source(tmp_path / "src", repo, sha))
    assert draft.id == "fixture_001"
    assert draft.commit == sha
    assert draft.category is Category.OFF_BY_ONE
    assert draft.difficulty is Difficulty.EASY
    assert draft.env.install == "true" and draft.env.test_timeout_seconds == 120
    assert draft.description.startswith("clamp(15, 0, 10)")
    assert draft.bug_patch == BUG_PATCH and draft.hidden_patch == HIDDEN_TEST_PATCH
    assert draft.gold_symbols is None


def test_load_draft_without_hidden_patch(tmp_path: Path, fixture_repo: tuple[Path, str]) -> None:
    repo, sha = fixture_repo
    draft = load_draft(write_source(tmp_path / "src", repo, sha, hidden_patch=None))
    assert draft.hidden_patch is None


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: (d / "bug.patch").unlink(), "missing bug.patch"),
        (lambda d: (d / "description.md").write_text(""), "description.md is empty"),
        (lambda d: (d / "bug.patch").write_text("not a diff"), "not a unified diff"),
        (
            lambda d: (d / "task.toml").write_text(
                (d / "task.toml").read_text().replace("off_by_one", "typo")
            ),
            "'typo' is not a valid Category",
        ),
        (
            lambda d: (d / "task.toml").write_text(
                (d / "task.toml").read_text().replace("[env]", "bogus = 1\n\n[env]")
            ),
            "unknown keys",
        ),
        (
            lambda d: (d / "task.toml").write_text(
                (d / "task.toml").read_text().replace('test_command = "pytest tests"\n', "")
            ),
            "missing keys",
        ),
    ],
)
def test_load_draft_rejects_broken_sources(
    tmp_path: Path, fixture_repo: tuple[Path, str], mutate, message: str
) -> None:
    repo, sha = fixture_repo
    source = write_source(tmp_path / "src", repo, sha)
    mutate(source)
    with pytest.raises(AuthoringError, match=message):
        load_draft(source)


# -- normalization (host-side git, no Docker) ------------------------------------


def test_normalize_derives_gold_patch_files_and_symbols(
    tmp_path: Path, fixture_repo: tuple[Path, str]
) -> None:
    repo, sha = fixture_repo
    draft = load_draft(write_source(tmp_path / "src", repo, sha))
    patches = normalize(draft, cache_dir=tmp_path / "cache")

    assert patches.gold_files == ("fixturepkg/__init__.py",)
    assert patches.gold_symbols == ("clamp",)
    assert patches.hidden_files == ("tests/test_hidden.py",)
    assert "-    return max(low, min(value, high))" in patches.bug_patch
    assert "+    return max(low, min(value, high - 1))" in patches.bug_patch
    assert "-    return max(low, min(value, high - 1))" in patches.gold_patch
    assert "+    return max(low, min(value, high))" in patches.gold_patch
    assert patches.hidden_patch is not None and "new file mode" in patches.hidden_patch
    assert patches.bug_patch.startswith("diff --git a/fixturepkg/__init__.py")


def test_normalize_rejects_hidden_patch_that_edits_existing_files(
    tmp_path: Path, fixture_repo: tuple[Path, str]
) -> None:
    repo, sha = fixture_repo
    editing_existing = "\n".join(
        [
            "diff --git a/tests/test_clamp.py b/tests/test_clamp.py",
            "--- a/tests/test_clamp.py",
            "+++ b/tests/test_clamp.py",
            "@@ -1,4 +1,5 @@",
            " from fixturepkg import clamp",
            "+import sys",
            " ",
            " ",
            " def test_inside():",
            "",
        ]
    )
    draft = load_draft(write_source(tmp_path / "src", repo, sha, hidden_patch=editing_existing))
    with pytest.raises(AuthoringError, match="must only add new files"):
        normalize(draft, cache_dir=tmp_path / "cache")


def test_normalize_rejects_mutations_in_test_files(
    tmp_path: Path, fixture_repo: tuple[Path, str]
) -> None:
    repo, sha = fixture_repo
    # Full three-line context: git apply anchors short-context hunks to file boundaries.
    test_mutation = "\n".join(
        [
            "diff --git a/tests/test_clamp.py b/tests/test_clamp.py",
            "--- a/tests/test_clamp.py",
            "+++ b/tests/test_clamp.py",
            "@@ -6,7 +6,7 @@ def test_inside():",
            " ",
            " ",
            " def test_above():",
            "-    assert clamp(15, 0, 10) == 10",
            "+    assert clamp(15, 0, 10) == 9",
            " ",
            " ",
            " def test_below():",
            "",
        ]
    )
    draft = load_draft(write_source(tmp_path / "src", repo, sha, bug_patch=test_mutation))
    with pytest.raises(AuthoringError, match="must only touch source files"):
        normalize(draft, cache_dir=tmp_path / "cache")


def test_normalize_rejects_patch_that_does_not_apply(
    tmp_path: Path, fixture_repo: tuple[Path, str]
) -> None:
    repo, sha = fixture_repo
    stale = BUG_PATCH.replace("high))", "high)) # nope")
    draft = load_draft(write_source(tmp_path / "src", repo, sha, bug_patch=stale))
    with pytest.raises(AuthoringError, match="bug.patch does not apply cleanly"):
        normalize(draft, cache_dir=tmp_path / "cache")


# -- templates and CLI --init ------------------------------------------------------


def test_init_draft_writes_templates_once(tmp_path: Path) -> None:
    target = tmp_path / "sources" / "demo_001"
    first = init_draft(target, task_id="demo_001", repo="https://x/y", commit="a" * 40)
    assert {p.name for p in first} == {"task.toml", "description.md"}
    assert 'id = "demo_001"' in (target / "task.toml").read_text()
    assert init_draft(target, task_id="demo_001", repo="https://x/y", commit="a" * 40) == []


def test_make_task_cli_init(tmp_path: Path) -> None:
    pythonpath = os.pathsep.join(p for p in (str(REPO_ROOT), os.environ.get("PYTHONPATH")) if p)
    env = {**os.environ, "PYTHONPATH": pythonpath}
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "make_task.py"),
            "--init",
            str(tmp_path / "cachetools_001"),
            "--id",
            "cachetools_001",
            "--repo",
            "https://github.com/tkem/cachetools",
            "--commit",
            "b" * 40,
            "--test-command",
            "pytest tests/test_lru.py",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "cachetools_001" / "task.toml").exists()
    assert (
        'test_command = "pytest tests/test_lru.py"'
        in (tmp_path / "cachetools_001" / "task.toml").read_text()
    )
