"""Unit tests for the agent tool surface (repopilot/tools): no Docker, no model.

A small git repository stands in for a task's buggy tree; ``run_tests`` is
exercised against a fake sandbox that records what it was asked to do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repopilot.sandbox.results import TestRun
from repopilot.tools import (
    TOOL_SPECS,
    PathError,
    Toolbox,
    Workspace,
    code,
    is_test_path,
    resolve_repo_path,
)
from tests.fakes import DEMO_FILES as FILES
from tests.fakes import FakeSandbox, make_run
from tests.gitfixtures import init_repo


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "repo"
    init_repo(root, FILES)
    return Workspace.from_repo(root)


# ----------------------------------------------------------------------------- paths


def test_resolve_repo_path_rejects_escapes_and_git(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("x = 1\n")
    assert resolve_repo_path(root, "pkg/a.py") == (root / "pkg" / "a.py").resolve()
    for bad in ["", " pkg/a.py", "/etc/passwd", "../outside", "pkg/../../x", ".git/config", "~/x"]:
        with pytest.raises(PathError):
            resolve_repo_path(root, bad)


def test_resolve_repo_path_rejects_symlinks_that_leave_the_repo(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("s")
    (root / "link.txt").symlink_to(tmp_path / "secret.txt")
    with pytest.raises(PathError, match="outside"):
        resolve_repo_path(root, "link.txt")


def test_is_test_path_conventions() -> None:
    assert is_test_path("tests/test_x.py") and is_test_path("pkg/test_x.py")
    assert is_test_path("pkg/tests/helpers.py") and is_test_path("conftest.py")
    assert is_test_path("pkg/x_test.py")
    assert not is_test_path("pkg/testing_utils.py") and not is_test_path("src/pkg/core.py")


# ------------------------------------------------------------------------- workspace


def test_workspace_files_read_write_diff_reset(workspace: Workspace) -> None:
    assert "demo/cache.py" in workspace.files() and "data.bin" in workspace.files()
    assert workspace.diff() == ""
    workspace.write_text(
        "demo/cache.py", workspace.read_text("demo/cache.py").replace("> self", ">= self")
    )
    assert workspace.changed_files() == ["demo/cache.py"]
    diff = workspace.diff()
    assert diff.startswith("diff --git a/demo/cache.py b/demo/cache.py")
    assert "-        if len(self.items) > self.size:" in diff
    assert "+        if len(self.items) >= self.size:" in diff
    workspace.write_text("demo/new.py", "x = 1\n")  # new files show up in the diff too
    assert "demo/new.py" in workspace.diff() and "demo/new.py" in workspace.files()
    version = workspace.version
    workspace.reset()
    assert workspace.diff() == "" and not workspace.exists("demo/new.py")
    assert workspace.version > version


def test_workspace_refuses_binary_and_missing_files(workspace: Workspace) -> None:
    with pytest.raises(PathError, match="binary"):
        workspace.read_text("data.bin")
    with pytest.raises(PathError, match="does not exist"):
        workspace.read_text("demo/nope.py")
    assert not workspace.exists("../etc/passwd")


def test_workspace_create_applies_the_bug_patch(tmp_path: Path) -> None:
    from tests.fixture_repo import BUG_PATCH
    from tests.fixture_repo import FILES as FIXTURE_FILES

    origin = tmp_path / "origin"
    sha = init_repo(origin, FIXTURE_FILES)
    with Workspace.create(str(origin), sha, BUG_PATCH, cache_dir=tmp_path / "cache") as ws:
        assert "high - 1" in ws.read_text("fixturepkg/__init__.py")  # buggy tree is HEAD
        assert ws.diff() == ""
        root = ws.root
    assert not root.exists()  # temp dir removed on close


# ------------------------------------------------------------------------- read_file


def test_read_file_pages_and_numbers_lines(workspace: Workspace) -> None:
    result = code.read_file(workspace, "demo/cache.py", start=9, end=12)
    assert not result.is_error
    assert result.output.splitlines()[0] == "demo/cache.py lines 9-12 of 21"
    assert "    9|     def put(self, key, value):" in result.output
    assert result.meta == {"path": "demo/cache.py", "start": 9, "end": 12, "lines": 21}

    whole = code.read_file(workspace, "demo/cache.py")
    assert whole.output.splitlines()[0] == "demo/cache.py lines 1-21 of 21"


def test_read_file_bounds_and_errors(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code, "MAX_READ_LINES", 5)
    result = code.read_file(workspace, "demo/cache.py", start=1, end=400)
    assert (
        result.output.splitlines()[0]
        == "demo/cache.py lines 1-5 of 21 (call again with start=6 for more)"
    )
    assert code.read_file(workspace, "demo/cache.py", start=0).is_error
    assert code.read_file(workspace, "demo/cache.py", start=5, end=2).is_error
    assert "only 21 lines" in code.read_file(workspace, "demo/cache.py", start=99).output
    assert "does not exist" in code.read_file(workspace, "demo/missing.py").output
    assert "must not contain" in code.read_file(workspace, "../x").output


# ----------------------------------------------------------------------- search_code


def test_search_code_is_case_insensitive_and_lists_source_before_tests(
    workspace: Workspace,
) -> None:
    result = code.search_code(workspace, "cache(")
    assert not result.is_error
    lines = result.output.splitlines()
    assert lines[0].startswith("demo/cache.py:")
    assert any(line.startswith("tests/test_cache.py:") for line in lines)
    assert lines.index(next(line for line in lines if line.startswith("tests/"))) > 0
    assert result.meta["hits"] == result.meta["shown"] == len(lines)
    assert "data.bin" not in result.output  # binaries are never searched


def test_search_code_top_k_regex_and_no_match(workspace: Workspace) -> None:
    limited = code.search_code(workspace, "self", top_k=2)
    assert len(limited.output.splitlines()) == 3 and "more matches" in limited.output
    assert limited.meta["shown"] == 2 and limited.meta["hits"] > 2
    regex = code.search_code(workspace, r"def \w+\(self, key", regex=True)
    assert "demo/cache.py:9:" in regex.output and "demo/cache.py:14:" in regex.output
    assert code.search_code(workspace, "[unbalanced", regex=True).is_error
    assert code.search_code(workspace, "zzz_not_here").output == "no matches for 'zzz_not_here'"
    assert code.search_code(workspace, "  ").is_error


# --------------------------------------------------------------------- search_symbol


def test_symbol_index_finds_functions_methods_classes_and_variables(workspace: Workspace) -> None:
    index = code.SymbolIndex(workspace)
    kinds = {s.qualname: s.kind for s in index.symbols}
    assert kinds["Cache"] == "class"
    assert kinds["Cache.put"] == "method" and kinds["Cache.get"] == "method"
    assert kinds["make_cache"] == "function" and kinds["make_cache.build"] == "function"
    assert kinds["DEFAULT_SIZE"] == "variable"
    assert kinds["helper"] == "function" and kinds["json"] == "variable"  # guarded by try/except

    result = code.search_symbol(workspace, index, "put")
    assert result.output == "demo/cache.py:9-12  method  Cache.put"
    assert code.search_symbol(workspace, index, "Cache.get").output.endswith("Cache.get")
    assert code.search_symbol(workspace, index, "Cache", kind="class").meta["hits"] == 1
    by_kind = code.search_symbol(workspace, index, "Cache", kind="function")
    assert by_kind.meta["exact"] is False  # only make_cache / make_cache.build resemble it
    assert code.search_symbol(workspace, index, "x", kind="nope").is_error


def test_search_symbol_falls_back_to_similar_names_and_tracks_edits(workspace: Workspace) -> None:
    index = code.SymbolIndex(workspace)
    similar = code.search_symbol(workspace, index, "cach")
    assert similar.output.startswith("no exact definition of 'cach'; similar names:")
    assert similar.meta["exact"] is False
    assert "no definition of 'nothing'" in code.search_symbol(workspace, index, "nothing").output

    workspace.write_text(
        "demo/cache.py", workspace.read_text("demo/cache.py") + "\n\ndef clear():\n    pass\n"
    )
    assert code.search_symbol(workspace, index, "clear").meta["hits"] == 1  # index refreshed
    workspace.write_text("demo/broken.py", "def (:\n")
    assert index.symbols  # rebuild
    assert index.unparseable == ["demo/broken.py"]


# ------------------------------------------------------------------- find_references


def test_find_references_matches_whole_words_and_qualified_names(workspace: Workspace) -> None:
    result = code.find_references(workspace, "Cache")
    assert "demo/__init__.py:1:" in result.output
    assert "tests/test_cache.py:" in result.output
    assert all("Cache" in line for line in result.output.splitlines())
    assert "def make_cache" not in result.output  # case-sensitive, whole word
    dotted = code.find_references(workspace, "Cache.put")
    assert "demo/cache.py:9:" in dotted.output and "tests/test_cache.py:" in dotted.output
    assert code.find_references(workspace, "nonexistent_name").meta["hits"] == 0


# ------------------------------------------------------------------------- edit_file


def test_edit_file_replaces_exactly_one_occurrence(workspace: Workspace) -> None:
    result = code.edit_file(
        workspace,
        "demo/cache.py",
        "        if len(self.items) > self.size:",
        "        if len(self.items) >= self.size:",
    )
    assert not result.is_error
    assert result.output.startswith("edited demo/cache.py at line 10; the region now reads:")
    assert "   10|         if len(self.items) >= self.size:" in result.output
    assert result.meta == {
        "path": "demo/cache.py",
        "line": 10,
        "removed_lines": 1,
        "added_lines": 1,
    }
    assert ">= self.size" in workspace.read_text("demo/cache.py")
    assert workspace.changed_files() == ["demo/cache.py"]


def test_edit_file_refuses_ambiguous_missing_and_test_edits(workspace: Workspace) -> None:
    assert (
        "occurs 2 times"
        in code.edit_file(workspace, "demo/cache.py", "size=DEFAULT_SIZE", "x").output
    )
    assert "was not found" in code.edit_file(workspace, "demo/cache.py", "nope", "x").output
    assert "read-only" in code.edit_file(workspace, "tests/test_cache.py", "Cache", "X").output
    assert "identical" in code.edit_file(workspace, "demo/cache.py", "Cache", "Cache").output
    assert "must not be empty" in code.edit_file(workspace, "demo/cache.py", "", "x").output
    assert "does not exist" in code.edit_file(workspace, "demo/none.py", "a", "b").output
    assert "must not contain" in code.edit_file(workspace, "../x.py", "a", "b").output
    assert workspace.diff() == ""  # nothing above changed the tree


# --------------------------------------------------------------------------- toolbox


def test_toolbox_specs_are_valid_schemas_with_the_six_tools() -> None:
    names = [spec.name for spec in TOOL_SPECS]
    assert names == [
        "search_code",
        "search_symbol",
        "find_references",
        "read_file",
        "edit_file",
        "run_tests",
    ]
    for spec in TOOL_SPECS:
        assert spec.parameters["type"] == "object"
        assert spec.parameters["additionalProperties"] is False
        assert set(spec.parameters["required"]) <= set(spec.parameters["properties"])
        assert spec.description


def test_toolbox_dispatch_validates_arguments(workspace: Workspace) -> None:
    box = Toolbox(workspace, sandbox=None, test_command="pytest tests")
    assert box.call("read_file", {"path": "demo/cache.py", "start": 1, "end": 2}).output.startswith(
        "demo/cache.py lines 1-2"
    )
    unknown = box.call("delete_repo", {})
    assert unknown.is_error and unknown.meta.get("invalid") and "unknown tool" in unknown.output
    missing = box.call("read_file", {})
    assert missing.is_error and "missing required argument(s): path" in missing.output
    extra = box.call("read_file", {"path": "demo/cache.py", "lines": 3})
    assert "unknown argument(s) lines" in extra.output
    typed = box.call("read_file", {"path": "demo/cache.py", "start": "1"})
    assert "start must be an integer" in typed.output
    enum = box.call("search_symbol", {"name": "Cache", "kind": "struct"})
    assert "kind must be one of" in enum.output
    assert box.call("read_file", None).is_error
    assert box.call("run_tests", {}).is_error and "no sandbox" in box.call("run_tests", {}).output


def test_toolbox_clips_long_outputs(workspace: Workspace) -> None:
    box = Toolbox(workspace, sandbox=None, test_command="pytest", max_output_chars=80)
    result = box.call("read_file", {"path": "demo/cache.py"})
    assert result.output.endswith("[output truncated at 80 characters]")
    assert result.meta["truncated"] is True


def test_run_tests_resets_applies_edits_and_summarizes(workspace: Workspace) -> None:
    run = make_run(
        **{
            "tests/test_cache.py::test_put_evicts": "failed",
            "tests/test_cache.py::test_other": "passed",
        }
    )
    sandbox = FakeSandbox(run)
    box = Toolbox(workspace, sandbox, test_command="pytest tests/test_cache.py", test_timeout=42)
    box.call(
        "edit_file",
        {"path": "demo/cache.py", "old_string": "> self.size", "new_string": ">= self.size"},
    )

    result = box.call("run_tests", {})
    kinds = [c[0] for c in sandbox.calls]
    assert kinds == ["exec", "apply", "run"]  # reset, then the diff, then the tests
    assert "git reset -q --hard HEAD" in sandbox.calls[0][1][-1]
    assert "+        if len(self.items) >= self.size:" in sandbox.calls[1][1]
    assert sandbox.calls[2][1] == "pytest tests/test_cache.py"
    assert not result.is_error
    assert (
        result.output.splitlines()[0]
        == "pytest tests/test_cache.py: 1 passed, 1 failed, 0 errors, 0 skipped (0.5s)"
    )
    assert "FAILED tests/test_cache.py::test_put_evicts" in result.output
    assert "AssertionError" in result.output
    assert result.meta["failed"] == ["tests/test_cache.py::test_put_evicts"]
    assert result.meta["patch_applied"] is True
    assert box.test_runs == 1


def test_run_tests_target_validation(workspace: Workspace) -> None:
    box = Toolbox(workspace, FakeSandbox(make_run()), test_command="pytest tests")
    assert box.test_command_for(None) == "pytest tests"
    assert box.test_command_for("pytest") == "pytest tests"
    assert (
        box.test_command_for("tests/test_cache.py::test_put_evicts")
        == "pytest tests/test_cache.py::test_put_evicts"
    )
    assert (
        box.test_command_for("pytest tests/test_cache.py demo") == "pytest tests/test_cache.py demo"
    )
    with pytest.raises(PathError, match="options"):
        box.test_command_for("tests -x")
    with pytest.raises(PathError, match="does not exist"):
        box.test_command_for("tests/test_missing.py")
    with pytest.raises(PathError, match="must not contain"):
        box.test_command_for("../other/tests")
    assert box.call("run_tests", {"target": "tests -k foo"}).is_error


def test_run_tests_reports_apply_failures_and_missing_reports(workspace: Workspace) -> None:
    box = Toolbox(workspace, FakeSandbox(make_run(), apply_ok=False), test_command="pytest tests")
    box.call(
        "edit_file",
        {"path": "demo/cache.py", "old_string": "> self.size", "new_string": ">= self.size"},
    )
    failed = box.call("run_tests", {})
    assert failed.is_error and "could not be applied" in failed.output

    crashed = TestRun(
        "pytest tests", 2, False, 0.3, "", "SyntaxError: invalid syntax", report_found=False
    )
    box = Toolbox(workspace, FakeSandbox(crashed), test_command="pytest tests")
    result = box.call("run_tests", {})
    assert result.is_error and "no report" in result.output and "SyntaxError" in result.output
    assert result.meta["report_found"] is False
