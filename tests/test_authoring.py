"""Task authoring without Docker: source loading, patch normalization, symbol lookup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evals.benchmark.authoring import (
    AuthoringError,
    audit_draft,
    changed_lines,
    classify_tests,
    enclosing_symbols,
    init_draft,
    is_test_path,
    load_draft,
    normalize,
    refresh_task,
)
from evals.benchmark.schema import Category, Difficulty, ReportLevel, Suite, TaskSource
from repopilot.sandbox.results import TestOutcome, TestResult, TestRun
from tests.fixture_repo import (
    BUG_PATCH,
    HIDDEN_TEST_PATCH,
    create_fixture_repo,
    fixture_task_dict,
)
from tests.gitfixtures import commit_all

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
    assert draft.authored_difficulty is Difficulty.EASY  # `difficulty` is the pre-v1 spelling
    assert draft.source is TaskSource.MUTATION
    assert draft.suite is Suite.V0 and draft.report_level is ReportLevel.INTERNAL
    assert draft.entry_points == () and draft.fix_commit is None
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


def test_normalize_captures_what_the_audit_needs(
    tmp_path: Path, fixture_repo: tuple[Path, str]
) -> None:
    repo, sha = fixture_repo
    draft = load_draft(write_source(tmp_path / "src", repo, sha))
    patches = normalize(draft, cache_dir=tmp_path / "cache")
    assert {s.qualname for s in patches.symbols} >= {"clamp", "test_inside"}
    assert "high - 1" in patches.gold_texts["fixturepkg/__init__.py"]  # the buggy side
    assert "fixturepkg/__init__.py" in patches.tree_files
    assert patches.changelog is None


# -- the audit ----------------------------------------------------------------------


def test_audit_draft_passes_an_internal_report_and_records_the_surface(
    tmp_path: Path, fixture_repo: tuple[Path, str]
) -> None:
    repo, sha = fixture_repo
    draft = load_draft(write_source(tmp_path / "src", repo, sha))
    patches = normalize(draft, cache_dir=tmp_path / "cache")
    audit = audit_draft(draft, patches)
    assert audit.report.ok
    assert audit.report.surface_symbols == ("clamp",)
    assert audit.report.surface_files == ("fixturepkg/__init__.py",)
    assert audit.report.cross_module is False  # the report names the changed function


def _toml_with(*extra: str) -> str:
    """TASK_TOML with top-level keys added above the [env] table."""
    return TASK_TOML.replace("\n[env]", "\n" + "\n".join(extra) + "\n\n[env]")


PUBLIC_API_TOML = _toml_with('suite = "v1"', 'report_level = "public_api"')


def test_audit_draft_refuses_a_public_api_report_naming_the_changed_symbol(
    tmp_path: Path, fixture_repo: tuple[Path, str]
) -> None:
    repo, sha = fixture_repo
    source = write_source(
        tmp_path / "src",
        repo,
        sha,
        toml=PUBLIC_API_TOML.format(id="fixture_001", repo=repo, commit=sha),
    )
    draft = load_draft(source)
    patches = normalize(draft, cache_dir=tmp_path / "cache")
    audit = audit_draft(draft, patches)
    assert not audit.report.ok
    assert any("changed symbol: 'clamp'" in v for v in audit.report.violations)
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    shipped = fixture_task_dict(repo, sha)
    shipped["description"] = draft.description
    (tasks_dir / "fixture_001.json").write_text(json.dumps(shipped))
    with pytest.raises(AuthoringError, match="violates its tier"):
        refresh_task(source, out_dir=tasks_dir, cache_dir=tmp_path / "cache")


def test_load_draft_checks_the_v1_keys(tmp_path: Path, fixture_repo: tuple[Path, str]) -> None:
    repo, sha = fixture_repo
    toml = _toml_with('suite = "v1"', 'report_level = "symptom_only"')
    source = write_source(
        tmp_path / "src", repo, sha, toml=toml.format(id="fixture_001", repo=repo, commit=sha)
    )
    with pytest.raises(AuthoringError, match="needs entry_points"):
        load_draft(source)
    toml = _toml_with('suite = "v1"', 'report_level = "symptom_only"', 'entry_points = ["clamp"]')
    (source / "task.toml").write_text(toml.format(id="fixture_001", repo=repo, commit=sha))
    draft = load_draft(source)
    assert draft.suite is Suite.V1 and draft.report_level is ReportLevel.SYMPTOM_ONLY
    assert draft.entry_points == ("clamp",)
    toml = _toml_with('fix_commit = "' + "c" * 40 + '"')
    (source / "task.toml").write_text(toml.format(id="fixture_001", repo=repo, commit=sha))
    with pytest.raises(AuthoringError, match="fix_commit is for real tasks"):
        load_draft(source)
    toml = _toml_with('hidden_pass_to_pass = "yes"')
    (source / "task.toml").write_text(toml.format(id="fixture_001", repo=repo, commit=sha))
    with pytest.raises(AuthoringError, match="hidden_pass_to_pass must be true or false"):
        load_draft(source)
    toml = _toml_with("hidden_pass_to_pass = true")
    (source / "task.toml").write_text(toml.format(id="fixture_001", repo=repo, commit=sha))
    assert load_draft(source).hidden_pass_to_pass is True


# -- classifying the two test runs ----------------------------------------------------


def _run(**outcomes: str) -> TestRun:
    tests = {
        nodeid.replace("__", "::"): TestResult(nodeid.replace("__", "::"), TestOutcome(outcome))
        for nodeid, outcome in outcomes.items()
    }
    return TestRun(
        command="pytest",
        exit_code=0,
        timed_out=False,
        duration_seconds=1.0,
        stdout="",
        stderr="",
        tests=tests,
        report_found=True,
    )


HIDDEN = "tests/test_repopilot_x.py"


def test_classify_tests_splits_fail_to_pass_from_pass_to_pass() -> None:
    buggy = _run(**{"tests/test_a.py__test_old": "passed", f"{HIDDEN}__test_new": "failed"})
    fixed = _run(**{"tests/test_a.py__test_old": "passed", f"{HIDDEN}__test_new": "passed"})
    f2p, p2p, excluded = classify_tests(buggy, fixed, hidden_files=[HIDDEN])
    assert f2p == (f"{HIDDEN}::test_new",)
    assert p2p == ("tests/test_a.py::test_old",)
    assert excluded == {}


def test_classify_tests_rejects_a_hidden_guard_unless_the_source_allows_it() -> None:
    buggy = _run(**{f"{HIDDEN}__test_new": "failed", f"{HIDDEN}__test_guard": "passed"})
    fixed = _run(**{f"{HIDDEN}__test_new": "passed", f"{HIDDEN}__test_guard": "passed"})
    with pytest.raises(AuthoringError, match="these pass with the bug too: .*test_guard"):
        classify_tests(buggy, fixed, hidden_files=[HIDDEN])
    f2p, p2p, _ = classify_tests(buggy, fixed, hidden_files=[HIDDEN], hidden_pass_to_pass=True)
    assert f2p == (f"{HIDDEN}::test_new",)
    assert p2p == (f"{HIDDEN}::test_guard",)


def test_classify_tests_rejects_broken_missing_and_useless_hidden_tests() -> None:
    both_fail = _run(**{f"{HIDDEN}__test_new": "failed", "tests/test_a.py__test_old": "passed"})
    with pytest.raises(AuthoringError, match="must fail with the bug and pass with the fix: "):
        classify_tests(both_fail, both_fail, hidden_files=[HIDDEN])
    with pytest.raises(AuthoringError, match="was not collected"):
        classify_tests(_run(a__t="passed"), _run(a__t="passed"), hidden_files=[HIDDEN])
    # A guard alone is not a hidden test: something must fail with the bug.
    guard_only = _run(**{f"{HIDDEN}__test_guard": "passed", "tests/test_a.py__test_old": "failed"})
    guard_only_fixed = _run(
        **{f"{HIDDEN}__test_guard": "passed", "tests/test_a.py__test_old": "passed"}
    )
    with pytest.raises(AuthoringError, match="no hidden test fails with the bug"):
        classify_tests(
            guard_only, guard_only_fixed, hidden_files=[HIDDEN], hidden_pass_to_pass=True
        )
    # The gold patch may not break a test that passes with the bug.
    with pytest.raises(AuthoringError, match="gold patch breaks tests"):
        classify_tests(
            _run(**{f"{HIDDEN}__test_new": "failed", "tests/test_a.py__test_old": "passed"}),
            _run(**{f"{HIDDEN}__test_new": "passed", "tests/test_a.py__test_old": "failed"}),
            hidden_files=[HIDDEN],
        )


# -- real tasks ---------------------------------------------------------------------


REAL_TOML = """\
id = "{id}"
repo = "{repo}"
commit = "{commit}"
source = "real"
fix_commit = "{fix}"
category = "off_by_one"
suite = "v1"
report_level = "public_api"
test_command = "pytest tests"

[env]
python = "3.11"
install = "true"
test_timeout_seconds = 120
"""


def _fix_the_fixture(repo: Path) -> tuple[str, str]:
    """Commit the bug into the fixture repo, then commit the fix with its test: (buggy, fix)."""
    buggy_src = (repo / "fixturepkg" / "__init__.py").read_text().replace("high))", "high - 1))")
    buggy = commit_all(repo, {"fixturepkg/__init__.py": buggy_src}, "introduce the bug")
    fixed_src = buggy_src.replace("high - 1))", "high))")
    fix = commit_all(
        repo,
        {
            "fixturepkg/__init__.py": fixed_src,
            "CHANGES.md": "- fix clamp upper bound\n",
            "tests/test_clamp.py": (repo / "tests" / "test_clamp.py").read_text()
            + "\n\ndef test_upper_bound_inclusive():\n    assert clamp(10, 0, 10) == 10\n",
        },
        "fix the upper bound",
    )
    return buggy, fix


def test_normalize_derives_a_real_task_from_its_fix_commit(
    tmp_path: Path, fixture_repo: tuple[Path, str]
) -> None:
    repo, _ = fixture_repo
    buggy, fix = _fix_the_fixture(repo)
    source = tmp_path / "src"
    source.mkdir()
    (source / "task.toml").write_text(
        REAL_TOML.format(id="fixture_002", repo=repo, commit=buggy, fix=fix)
    )
    (source / "hidden.patch").write_text(HIDDEN_TEST_PATCH)
    (source / "description.md").write_text("Clamping 10 into [0, 10] gives 9.\n")
    draft = load_draft(source)
    assert draft.source is TaskSource.REAL and draft.bug_patch is None
    patches = normalize(draft, cache_dir=tmp_path / "cache")
    assert patches.bug_patch is None
    assert patches.gold_files == ("fixturepkg/__init__.py",)  # tests and CHANGES.md excluded
    assert "+    return max(low, min(value, high))" in patches.gold_patch
    assert patches.gold_symbols == ("clamp",)
    assert patches.changelog == "- fix clamp upper bound\n" or patches.changelog is None
    audit = audit_draft(draft, patches)
    assert audit.report.ok and audit.report.cross_module is False or audit.report.cross_module


def test_normalize_rejects_a_real_task_whose_commit_is_not_the_fix_parent(
    tmp_path: Path, fixture_repo: tuple[Path, str]
) -> None:
    repo, base = fixture_repo
    _, fix = _fix_the_fixture(repo)
    source = tmp_path / "src"
    source.mkdir()
    (source / "task.toml").write_text(
        REAL_TOML.format(id="fixture_002", repo=repo, commit=base, fix=fix)
    )
    (source / "hidden.patch").write_text(HIDDEN_TEST_PATCH)
    (source / "description.md").write_text("Clamping 10 into [0, 10] gives 9.\n")
    with pytest.raises(AuthoringError, match="must be the fix's parent"):
        normalize(load_draft(source), cache_dir=tmp_path / "cache")


# -- refresh (no Docker) ------------------------------------------------------------


def test_refresh_task_rederives_the_audit_fields_and_keeps_the_test_lists(
    tmp_path: Path, fixture_repo: tuple[Path, str]
) -> None:
    repo, sha = fixture_repo
    source = write_source(tmp_path / "src", repo, sha)
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    shipped = fixture_task_dict(repo, sha)
    shipped["description"] = (source / "description.md").read_text().strip()
    shipped["difficulty"] = "hard"  # the authored label of a pre-v1 file
    (tasks_dir / "fixture_001.json").write_text(json.dumps(shipped))
    result = refresh_task(source, out_dir=tasks_dir, cache_dir=tmp_path / "cache")
    task = result.task
    assert task.fail_to_pass == shipped["fail_to_pass"]
    assert task.pass_to_pass == shipped["pass_to_pass"]
    assert task.gold_patch == shipped["gold_patch"]  # the sandbox-derived text is kept
    assert task.surface_files == ["fixturepkg/__init__.py"]
    assert task.cross_module is False and task.hidden_only is False
    assert task.difficulty is Difficulty.EASY  # derived: no hardness factor
    assert task.suite is Suite.V0 and task.report_level is ReportLevel.INTERNAL
    assert json.loads((tasks_dir / "fixture_001.json").read_text())["difficulty"] == "easy"
    assert result.derived is None and result.image is None


def test_refresh_task_refuses_when_the_fix_changed(
    tmp_path: Path, fixture_repo: tuple[Path, str]
) -> None:
    repo, sha = fixture_repo
    source = write_source(tmp_path / "src", repo, sha)
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    shipped = fixture_task_dict(repo, sha)
    shipped["description"] = "a different report"
    (tasks_dir / "fixture_001.json").write_text(json.dumps(shipped))
    with pytest.raises(AuthoringError, match="re-derived with make_task"):
        refresh_task(source, out_dir=tasks_dir, cache_dir=tmp_path / "cache")


# -- templates and CLI --init ------------------------------------------------------


def test_init_draft_writes_templates_once(tmp_path: Path) -> None:
    target = tmp_path / "sources" / "demo_001"
    first = init_draft(target, task_id="demo_001", repo="https://x/y", commit="a" * 40)
    assert {p.name for p in first} == {"task.toml", "description.md"}
    toml = (target / "task.toml").read_text()
    assert 'id = "demo_001"' in toml
    assert 'suite = "v1"' in toml and 'report_level = "public_api"' in toml
    assert "source =" not in toml
    assert init_draft(target, task_id="demo_001", repo="https://x/y", commit="a" * 40) == []


def test_init_draft_for_a_real_task(tmp_path: Path) -> None:
    target = tmp_path / "sources" / "demo_009"
    init_draft(target, task_id="demo_009", repo="https://x/y", commit="a" * 40, fix_commit="b" * 40)
    toml = (target / "task.toml").read_text()
    assert 'source = "real"' in toml and f'fix_commit = "{"b" * 40}"' in toml


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
