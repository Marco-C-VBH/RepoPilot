"""Authoring RepoPilot-Bench tasks from a source directory (see scripts/make_task.py).

A task *source* directory holds only what a human has to write:

    task.toml        id, repo, commit, category, test_command, suite, report_level,
                     [entry_points], [env]; real tasks: source = "real", fix_commit
    bug.patch        clean tree -> buggy tree: the injected mutation (mutation tasks)
    hidden.patch     adds regression test file(s); applied at evaluation only
    description.md   the bug report the agent sees

Everything else in the task JSON is derived by running the code, never typed:

* ``gold_patch`` -- the exact reverse of the mutation (``git diff -R``), or for
  a real task the source hunks of ``fix_commit`` against its parent;
* ``gold_files`` / ``gold_symbols`` -- from the patch hunks and the AST of the
  buggy files (innermost function or class containing each changed line);
* ``fail_to_pass`` / ``pass_to_pass`` -- from two sandbox runs, buggy+hidden
  versus fixed+hidden, the way SWE-bench derives FAIL_TO_PASS / PASS_TO_PASS;
* ``surface_symbols`` / ``surface_files`` / ``cross_module`` / ``difficulty`` --
  from the report audit (``evals.benchmark.audit``) against the buggy tree's
  symbol table, then the rule of docs/bench-v1-design.md §7.

Packaging mistakes are rejected here instead of surfacing in a benchmark run:
a hidden test that passes with the bug, a gold patch that breaks an existing
test, a hidden patch that edits an existing file, a mutation that touches test
files, a test command that never collects the hidden tests, a report that
names what its tier forbids.
"""

from __future__ import annotations

import ast
import re
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from evals.benchmark.audit import (
    AuditResult,
    SymbolTable,
    audit_report,
    find_changelog,
    site_notes,
)
from evals.benchmark.schema import (
    Category,
    Difficulty,
    ReportLevel,
    Suite,
    Task,
    TaskEnv,
    TaskSource,
    derive_difficulty,
    looks_like_unified_diff,
    patch_shape,
    touched_files,
)
from evals.harness import with_hidden_files
from repopilot.sandbox.docker import ImageSpec, Sandbox, build_task_image
from repopilot.sandbox.limits import DEFAULT_LIMITS, SandboxLimits
from repopilot.sandbox.repo import (
    DEFAULT_CACHE_DIR,
    changed_paths,
    diff_between,
    export_tree,
    parent_commit,
)
from repopilot.sandbox.results import TestOutcome, TestRun
from repopilot.tools.code import Symbol
from repopilot.tools.paths import is_test_path
from repopilot.tools.workspace import Workspace

SOURCES_DIR = Path(__file__).resolve().parent / "sources"
REQUIRED_FILES = ("task.toml", "description.md")
BUG_PATCH_FILE = "bug.patch"
HIDDEN_PATCH_FILE = "hidden.patch"
REQUIRED_KEYS = ("id", "repo", "commit", "category", "test_command")
OPTIONAL_KEYS = (
    "env",
    "gold_symbols",
    "source",
    "suite",
    "report_level",
    "entry_points",
    "difficulty",  # the pre-v1 spelling of authored_difficulty
    "authored_difficulty",
    "site_note",
    "fix_commit",
    "hidden_pass_to_pass",
)
GIT_IDENTITY = ("-c", "user.name=repopilot", "-c", "user.email=repopilot@localhost")
MAX_CHANGELOG_CHARS = 200_000

_FILE_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SKIP_PREFIXES = (
    "--- ",
    "+++ ",
    "index ",
    "new file",
    "deleted file",
    "similarity",
    "rename",
    "\\",
)


class AuthoringError(ValueError):
    """The task source is incomplete, inconsistent, or does not behave as claimed."""


# -- source directory ------------------------------------------------------------


@dataclass(frozen=True)
class TaskDraft:
    """The human-written half of a task, loaded from a source directory."""

    directory: Path
    id: str
    repo: str
    commit: str
    category: Category
    test_command: str
    env: TaskEnv
    description: str
    bug_patch: str | None  # None for a real task: the base tree is the buggy tree
    hidden_patch: str | None
    gold_symbols: tuple[str, ...] | None = None
    source: TaskSource = TaskSource.MUTATION
    suite: Suite = Suite.V0
    report_level: ReportLevel = ReportLevel.INTERNAL
    entry_points: tuple[str, ...] = ()
    authored_difficulty: Difficulty | None = None  # the author's estimate; never shipped
    site_note: str | None = None  # why a site the audit flagged was kept
    fix_commit: str | None = None  # real tasks: the upstream fix
    # Hidden tests that pass with the bug too are allowed (as pass_to_pass) only when
    # the author says so: regression guards transplanted with a real fix, never an
    # accident.
    hidden_pass_to_pass: bool = False


def load_draft(directory: Path) -> TaskDraft:
    directory = Path(directory)
    if not directory.is_dir():
        raise AuthoringError(f"task source directory not found: {directory}")
    missing = [name for name in REQUIRED_FILES if not (directory / name).is_file()]
    if missing:
        raise AuthoringError(f"{directory}: missing {', '.join(missing)}")

    try:
        meta = tomllib.loads((directory / "task.toml").read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise AuthoringError(f"{directory / 'task.toml'}: {exc}") from exc
    missing_keys = [key for key in REQUIRED_KEYS if key not in meta]
    if missing_keys:
        raise AuthoringError(f"task.toml: missing keys {missing_keys}")
    unknown = sorted(set(meta) - set(REQUIRED_KEYS) - set(OPTIONAL_KEYS))
    if unknown:
        raise AuthoringError(f"task.toml: unknown keys {unknown}")

    try:
        env = TaskEnv.model_validate(meta.get("env", {}))
        category = Category(meta["category"])
        source = TaskSource(meta.get("source", "mutation"))
        suite = Suite(meta.get("suite", "v0"))
        report_level = ReportLevel(meta.get("report_level", "internal"))
        authored = meta.get("authored_difficulty", meta.get("difficulty"))
        authored_difficulty = Difficulty(authored) if authored is not None else None
    except (ValidationError, ValueError) as exc:
        raise AuthoringError(f"task.toml: {exc}") from exc

    description = (directory / "description.md").read_text(encoding="utf-8").strip()
    if not description:
        raise AuthoringError("description.md is empty")

    fix_commit = meta.get("fix_commit")
    if source is TaskSource.MUTATION:
        if fix_commit is not None:
            raise AuthoringError("task.toml: fix_commit is for real tasks (source = 'real')")
        if not (directory / BUG_PATCH_FILE).is_file():
            raise AuthoringError(f"{directory}: missing {BUG_PATCH_FILE}")
        bug_patch: str | None = _read_patch(directory / BUG_PATCH_FILE)
    else:
        if not isinstance(fix_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", fix_commit):
            raise AuthoringError("task.toml: a real task needs fix_commit (full 40-char SHA)")
        if (directory / BUG_PATCH_FILE).is_file():
            raise AuthoringError("task.toml: a real task has no bug.patch (the base tree is buggy)")
        bug_patch = None
    hidden_path = directory / HIDDEN_PATCH_FILE
    hidden_patch = _read_patch(hidden_path) if hidden_path.is_file() else None

    symbols = meta.get("gold_symbols")
    if symbols is not None and (
        not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols)
    ):
        raise AuthoringError("task.toml: gold_symbols must be a list of strings")
    entry_points = meta.get("entry_points", [])
    if not isinstance(entry_points, list) or not all(isinstance(e, str) for e in entry_points):
        raise AuthoringError("task.toml: entry_points must be a list of strings")
    if report_level is ReportLevel.SYMPTOM_ONLY and not entry_points:
        raise AuthoringError("task.toml: a symptom_only report needs entry_points")
    site_note = meta.get("site_note")
    if site_note is not None and not isinstance(site_note, str):
        raise AuthoringError("task.toml: site_note must be a string")
    hidden_pass_to_pass = meta.get("hidden_pass_to_pass", False)
    if not isinstance(hidden_pass_to_pass, bool):
        raise AuthoringError("task.toml: hidden_pass_to_pass must be true or false")

    return TaskDraft(
        directory=directory,
        id=str(meta["id"]),
        repo=str(meta["repo"]),
        commit=str(meta["commit"]),
        category=category,
        test_command=str(meta["test_command"]),
        env=env,
        description=description,
        bug_patch=bug_patch,
        hidden_patch=hidden_patch,
        gold_symbols=tuple(symbols) if symbols is not None else None,
        source=source,
        suite=suite,
        report_level=report_level,
        entry_points=tuple(entry_points),
        authored_difficulty=authored_difficulty,
        site_note=site_note,
        fix_commit=fix_commit,
        hidden_pass_to_pass=hidden_pass_to_pass,
    )


def _read_patch(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if not looks_like_unified_diff(text):
        raise AuthoringError(f"{path.name} is not a unified diff (needs '+++' and '@@' lines)")
    return text if text.endswith("\n") else text + "\n"


# -- patch normalization + localization targets ----------------------------------


@dataclass(frozen=True)
class NormalizedPatches:
    bug_patch: str | None  # None for a real task
    gold_patch: str
    hidden_patch: str | None
    gold_files: tuple[str, ...]
    gold_symbols: tuple[str, ...]
    hidden_files: tuple[str, ...]
    symbols: tuple[Symbol, ...] = ()  # the buggy tree's symbol table (what the agent sees)
    gold_texts: dict[str, str] = field(default_factory=dict)  # gold files, buggy side
    changelog: str | None = None  # the repository's changelog at the pinned commit
    tree_files: tuple[str, ...] = ()


def _source_paths(paths: Sequence[str]) -> list[str]:
    """The paths of a fix that count as its source change: Python, not tests."""
    return [p for p in paths if p.endswith(".py") and not is_test_path(p)]


def normalize(draft: TaskDraft, *, cache_dir: Path = DEFAULT_CACHE_DIR) -> NormalizedPatches:
    """Re-express the human patches as git diffs and derive gold patch, files, symbols.

    Also captures what the audit needs from the tree the agent sees: its
    symbol table, the gold files' text and the changelog.
    """
    with tempfile.TemporaryDirectory(prefix="repopilot-author-") as tmp:
        work = Path(tmp) / "repo"
        export_tree(draft.repo, draft.commit, work, cache_dir=cache_dir)
        _git(work, "init", "-q", "-b", "main")
        _git(work, "add", "-A")
        _git(work, *GIT_IDENTITY, "commit", "-q", "--allow-empty", "-m", "clean")

        bug_patch: str | None
        if draft.source is TaskSource.MUTATION:
            assert draft.bug_patch is not None
            _apply(work, draft.bug_patch, BUG_PATCH_FILE)
            _git(work, *GIT_IDENTITY, "commit", "-q", "-m", "buggy")
            # Two commits, two plain diffs: clean -> buggy is the mutation, buggy -> clean
            # the fix.  (``git diff -R`` would swap the a/ b/ prefixes; this keeps them.)
            bug_patch = _git(work, "diff", "HEAD~1", "HEAD")
            gold_patch = _git(work, "diff", "HEAD", "HEAD~1")
        else:
            assert draft.fix_commit is not None
            bug_patch = None
            gold_patch = _real_gold_patch(draft, work, cache_dir=cache_dir)
        gold_files = tuple(sorted(touched_files(gold_patch)))
        if not gold_files:
            raise AuthoringError(
                f"{BUG_PATCH_FILE} changes nothing"
                if bug_patch is not None
                else "the fix commit changes no source file"
            )
        touched_tests = [f for f in gold_files if is_test_path(f)]
        if touched_tests:
            raise AuthoringError(
                f"{BUG_PATCH_FILE} must only touch source files, not tests: {touched_tests}"
            )

        changed = changed_lines(gold_patch)  # old side of the gold patch == the buggy tree
        symbols: list[str] = []
        gold_texts: dict[str, str] = {}
        for path in gold_files:
            file = work / path
            if not file.is_file():
                continue  # the mutation deleted it
            text = file.read_text(encoding="utf-8")
            gold_texts[path] = text
            try:
                found = enclosing_symbols(
                    text,
                    changed.modified.get(path, set()),
                    changed.inserted_before.get(path, set()),
                )
            except SyntaxError as exc:
                raise AuthoringError(f"{BUG_PATCH_FILE} leaves {path} unparseable: {exc}") from exc
            symbols += [s for s in found if s not in symbols]

        workspace = Workspace.from_repo(work)
        table = SymbolTable.from_workspace(workspace)
        tree_files = tuple(workspace.files())
        changelog_path = find_changelog(tree_files)
        changelog = None
        if changelog_path is not None:
            changelog = (work / changelog_path).read_text(encoding="utf-8", errors="replace")
            changelog = changelog[:MAX_CHANGELOG_CHARS]

        hidden_patch = None
        hidden_files: tuple[str, ...] = ()
        if draft.hidden_patch:
            _apply(work, draft.hidden_patch, HIDDEN_PATCH_FILE)
            status = _git(work, "diff", "--cached", "--name-status").strip()
            not_added = [line for line in status.splitlines() if not line.startswith("A")]
            if not_added:
                raise AuthoringError(
                    "hidden.patch must only add new files, so it can never conflict with a "
                    f"candidate patch; it changes existing files: {not_added}"
                )
            hidden_patch = _git(work, "diff", "--cached")
            hidden_files = tuple(sorted(touched_files(hidden_patch)))
            non_tests = [f for f in hidden_files if not is_test_path(f)]
            if non_tests:
                raise AuthoringError(f"hidden.patch should add test files only: {non_tests}")

    return NormalizedPatches(
        bug_patch=bug_patch,
        gold_patch=gold_patch,
        hidden_patch=hidden_patch,
        gold_files=gold_files,
        gold_symbols=tuple(symbols),
        hidden_files=hidden_files,
        symbols=table.symbols,
        gold_texts=gold_texts,
        changelog=changelog,
        tree_files=tree_files,
    )


def _real_gold_patch(draft: TaskDraft, work: Path, *, cache_dir: Path) -> str:
    """A real task's gold patch: the fix commit's source hunks against its parent.

    Checks that ``commit`` is the fix's parent, restricts the diff to Python
    source files (tests travel through hidden.patch, changelogs and docs are
    not part of the fix), and verifies it applies to the exported tree.
    """
    fix = draft.fix_commit
    assert fix is not None
    parent = parent_commit(draft.repo, fix, cache_dir)
    if parent != draft.commit:
        raise AuthoringError(
            f"a real task's commit must be the fix's parent: {fix[:12]}^ is {parent[:12]}, "
            f"task.toml says {draft.commit[:12]}"
        )
    paths = _source_paths(changed_paths(draft.repo, parent, fix, cache_dir))
    if not paths:
        raise AuthoringError(f"fix commit {fix[:12]} changes no Python source file")
    patch = diff_between(draft.repo, parent, fix, cache_dir=cache_dir, paths=tuple(paths))
    if not looks_like_unified_diff(patch):
        raise AuthoringError(f"fix commit {fix[:12]}: could not derive a unified diff")
    _apply(work, patch, "the fix commit's source diff")
    _git(work, "reset", "-q", "--hard", "HEAD")  # back to the buggy tree
    return patch


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=300
    )
    if proc.returncode != 0:
        raise AuthoringError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()}")
    return proc.stdout


def _apply(cwd: Path, patch: str, name: str) -> None:
    proc = subprocess.run(
        ["git", "apply", "--index", "--whitespace=nowarn", "-"],
        cwd=cwd,
        input=patch,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        message = f"{name} does not apply cleanly:\n{proc.stderr.strip()}"
        if "does not exist in index" in proc.stderr:
            message += (
                f"\nhint: {name} references files that are not in the target repository -- "
                "it was probably produced by `git diff` in a different repository. Run "
                "`git diff` inside the scratch clone (work/<repo>), and check with "
                f"`head -3 {name}` that the first path belongs to the target repo."
            )
        raise AuthoringError(message)


@dataclass(frozen=True)
class ChangedLines:
    """Line numbers on the *old* side of a diff: modified/removed, and insertion points."""

    modified: dict[str, set[int]] = field(default_factory=dict)
    inserted_before: dict[str, set[int]] = field(default_factory=dict)


def changed_lines(patch: str) -> ChangedLines:
    modified: dict[str, set[int]] = {}
    inserted: dict[str, set[int]] = {}
    current: str | None = None
    old = 0
    for line in patch.splitlines():
        header = _FILE_RE.match(line)
        if header:
            current = header.group(1)
            modified.setdefault(current, set())
            inserted.setdefault(current, set())
            continue
        if current is None:
            continue
        hunk = _HUNK_RE.match(line)
        if hunk:
            old = int(hunk.group(1))
            continue
        if line.startswith(_SKIP_PREFIXES):
            continue
        if line.startswith("-"):
            modified[current].add(old)
            old += 1
        elif line.startswith("+"):
            inserted[current].add(old)
        else:  # context line (" ..." or an empty line)
            old += 1
    return ChangedLines(modified=modified, inserted_before=inserted)


@dataclass(frozen=True)
class _Span:
    name: str
    start: int
    end: int


def _symbol_spans(text: str) -> list[_Span]:
    spans: list[_Span] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{prefix}{child.name}"
                decorators = [d.lineno for d in child.decorator_list]
                start = min([child.lineno, *decorators])
                spans.append(_Span(name, start, child.end_lineno or child.lineno))
                visit(child, name + ".")
            else:
                visit(child, prefix)

    visit(ast.parse(text), "")
    return spans


def enclosing_symbols(text: str, modified: set[int], inserted_before: set[int]) -> list[str]:
    """Innermost function/class (``Class.method`` style) containing each changed line.

    An insertion before line ``p`` is attributed to line ``p - 1`` when that line
    is inside a symbol (code appended at the end of a function), else to ``p``.
    """
    spans = _symbol_spans(text)

    def innermost(line: int) -> str | None:
        inside = [s for s in spans if s.start <= line <= s.end]
        return min(inside, key=lambda s: s.end - s.start).name if inside else None

    lines: list[int] = sorted(modified)
    for position in sorted(inserted_before):
        lines.append(position - 1 if position > 1 and innermost(position - 1) else position)

    names: list[str] = []
    for line in lines:
        name = innermost(line)
        if name and name not in names:
            names.append(name)
    return names


# -- test derivation -------------------------------------------------------------


@dataclass(frozen=True)
class DerivedTests:
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    excluded: dict[str, str]
    buggy_run: TestRun
    fixed_run: TestRun
    command: str


def derive_tests(
    draft: TaskDraft,
    patches: NormalizedPatches,
    *,
    image: str,
    limits: SandboxLimits = DEFAULT_LIMITS,
) -> DerivedTests:
    """Run the tests with the bug and with the fix; classify every collected test."""
    command = with_hidden_files(draft.test_command, patches.hidden_files)
    timeout = draft.env.test_timeout_seconds
    buggy = _run_with(image, [patches.hidden_patch], command, timeout, limits, "buggy")
    fixed = _run_with(
        image, [patches.gold_patch, patches.hidden_patch], command, timeout, limits, "fixed"
    )

    fail_to_pass, pass_to_pass, excluded = classify_tests(
        buggy,
        fixed,
        hidden_files=patches.hidden_files,
        hidden_pass_to_pass=draft.hidden_pass_to_pass,
        command=command,
    )
    return DerivedTests(
        fail_to_pass=fail_to_pass,
        pass_to_pass=pass_to_pass,
        excluded=excluded,
        buggy_run=buggy,
        fixed_run=fixed,
        command=command,
    )


def classify_tests(
    buggy: TestRun,
    fixed: TestRun,
    *,
    hidden_files: Sequence[str],
    hidden_pass_to_pass: bool = False,
    command: str = "",
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, str]]:
    """``(fail_to_pass, pass_to_pass, excluded)`` from the buggy and fixed runs.

    Rejects a gold patch that breaks a passing test, a hidden file that was not
    collected, a hidden test that fails on both sides, and -- unless the source
    says ``hidden_pass_to_pass = true`` -- a hidden test that passes with the bug
    (a regression guard transplanted with a real fix; it counts as pass_to_pass).
    At least one hidden test must fail with the bug and pass with the fix.
    """
    for hidden_file in hidden_files:
        if not any(t.startswith(hidden_file + "::") for t in (*buggy.tests, *fixed.tests)):
            raise AuthoringError(
                f"hidden test file {hidden_file} was not collected by {command!r}; "
                "check the file name and the test command"
            )

    fail_to_pass: list[str] = []
    pass_to_pass: list[str] = []
    excluded: dict[str, str] = {}
    broken: list[str] = []
    for nodeid in sorted(set(buggy.tests) | set(fixed.tests)):
        before = buggy.outcome(nodeid)
        after = fixed.outcome(nodeid)
        if after is TestOutcome.PASSED and before is TestOutcome.PASSED:
            pass_to_pass.append(nodeid)
        elif after is TestOutcome.PASSED:
            fail_to_pass.append(nodeid)
        elif before is TestOutcome.PASSED:
            broken.append(f"{nodeid} ({after})")
        else:
            excluded[nodeid] = f"{before or 'missing'} with bug, {after or 'missing'} with fix"
    if broken:
        raise AuthoringError(f"gold patch breaks tests that pass with the bug: {broken}")

    hidden = set(hidden_files)
    hidden_ids = sorted({t for t in (*buggy.tests, *fixed.tests) if _file_of(t) in hidden})
    guards = [t for t in hidden_ids if t in pass_to_pass]
    undetecting = [t for t in hidden_ids if t not in fail_to_pass and t not in guards]
    if undetecting:
        detail = ", ".join(
            f"{t}: {buggy.outcome(t) or 'missing'} with bug, "
            f"{fixed.outcome(t) or 'missing'} with fix"
            for t in undetecting
        )
        raise AuthoringError(f"hidden tests must fail with the bug and pass with the fix: {detail}")
    if guards and not hidden_pass_to_pass:
        raise AuthoringError(
            "hidden tests must fail with the bug and pass with the fix; these pass with the "
            f"bug too: {', '.join(guards)} (a regression guard transplanted with a real fix "
            "is allowed when task.toml says hidden_pass_to_pass = true; it then counts as "
            "pass_to_pass)"
        )
    if hidden_ids and not any(t in fail_to_pass for t in hidden_ids):
        raise AuthoringError("no hidden test fails with the bug and passes with the fix")
    if not fail_to_pass:
        raise AuthoringError("no test distinguishes the buggy tree from the fixed one")
    return tuple(fail_to_pass), tuple(pass_to_pass), excluded


def _file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _run_with(
    image: str,
    patches: Sequence[str | None],
    command: str,
    timeout: int,
    limits: SandboxLimits,
    label: str,
) -> TestRun:
    with Sandbox(image, limits) as sandbox:
        for patch in patches:
            if patch:
                applied = sandbox.apply_patch(patch)
                if not applied.ok:
                    raise AuthoringError(f"[{label}] patch failed to apply:\n{applied.stderr}")
        run = sandbox.run_tests(command, timeout=timeout)
    if run.timed_out:
        raise AuthoringError(
            f"[{label}] tests timed out after {timeout}s; narrow test_command or raise "
            "env.test_timeout_seconds"
        )
    if not run.report_found:
        raise AuthoringError(
            f"[{label}] no test report (exit {run.exit_code}); is test_command a pytest "
            f"command?\n{run.stderr[-2000:]}"
        )
    if run.collection_errors:
        raise AuthoringError(f"[{label}] collection errors: {list(run.collection_errors)}")
    return run


# -- the audit ---------------------------------------------------------------------


@dataclass(frozen=True)
class DraftAudit:
    report: AuditResult
    site: tuple[str, ...]  # site notes (informational)

    def format(self) -> str:
        lines = [self.report.format()]
        if self.site:
            lines.append("site audit (for the author; not enforced):")
            lines += [f"  {note}" for note in self.site]
        else:
            lines.append("site audit: nothing flagged")
        return "\n".join(lines)


def audit_draft(draft: TaskDraft, patches: NormalizedPatches) -> DraftAudit:
    """The report audit and the site audit of a draft against its buggy tree."""
    table = SymbolTable(patches.symbols)
    result = audit_report(
        draft.description,
        table,
        gold_files=patches.gold_files,
        gold_symbols=draft.gold_symbols or patches.gold_symbols,
        report_level=draft.report_level,
        entry_points=draft.entry_points,
    )
    notes = site_notes(
        patches.gold_patch,
        patches.gold_texts,
        gold_symbols=draft.gold_symbols or patches.gold_symbols,
        changelog=patches.changelog,
    )
    return DraftAudit(report=result, site=tuple(notes))


def _build_task(
    draft: TaskDraft,
    patches: NormalizedPatches,
    audit: DraftAudit,
    *,
    fail_to_pass: Sequence[str],
    pass_to_pass: Sequence[str],
) -> Task:
    """Assemble the task JSON from the human parts, the derivations and the audit."""
    hidden = set(patches.hidden_files)
    hidden_only = bool(hidden) and all(t.split("::", 1)[0] in hidden for t in fail_to_pass)
    difficulty = derive_difficulty(
        cross_module=audit.report.cross_module,
        hidden_only=hidden_only,
        multi_site=patch_shape(patches.gold_patch).multi_site,
        symptom_only=draft.report_level is ReportLevel.SYMPTOM_ONLY,
    )
    try:
        return Task(
            id=draft.id,
            repo=draft.repo,
            base_commit=draft.commit,
            source=draft.source,
            description=draft.description,
            bug_patch=patches.bug_patch,
            gold_patch=patches.gold_patch,
            hidden_test_patch=patches.hidden_patch,
            fail_to_pass=list(fail_to_pass),
            pass_to_pass=list(pass_to_pass),
            test_command=draft.test_command,
            gold_files=list(patches.gold_files),
            gold_symbols=list(draft.gold_symbols or patches.gold_symbols),
            category=draft.category,
            difficulty=difficulty,
            env=draft.env,
            suite=draft.suite,
            report_level=draft.report_level,
            entry_points=list(draft.entry_points),
            surface_symbols=list(audit.report.surface_symbols),
            surface_files=list(audit.report.surface_files),
            cross_module=audit.report.cross_module,
            fix_commit=draft.fix_commit,
        )
    except ValidationError as exc:
        raise AuthoringError(f"derived task violates the schema:\n{exc}") from exc


# -- orchestration ---------------------------------------------------------------


@dataclass(frozen=True)
class AuthoringResult:
    task: Task
    patches: NormalizedPatches
    derived: DerivedTests | None  # None for a metadata refresh (no test run)
    image: str | None
    path: Path | None
    audit: DraftAudit | None = None


def make_task(
    directory: Path,
    *,
    out_dir: Path,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    rebuild: bool = False,
    force: bool = False,
    dry_run: bool = False,
    limits: SandboxLimits = DEFAULT_LIMITS,
    log: Callable[[str], None] = print,
) -> AuthoringResult:
    """Turn a task source directory into a validated ``<id>.json`` in ``out_dir``."""
    draft = load_draft(directory)
    target = out_dir / f"{draft.id}.json"
    if target.exists() and not force and not dry_run:
        raise AuthoringError(f"{target} already exists; pass --force to overwrite it")

    log(f"[{draft.id}] normalizing patches against {draft.repo} @ {draft.commit[:12]}")
    patches = normalize(draft, cache_dir=cache_dir)
    log(f"  gold_files:   {', '.join(patches.gold_files)}")
    log(f"  gold_symbols: {', '.join(patches.gold_symbols) or '(none found)'}")
    if patches.hidden_files:
        log(f"  hidden tests: {', '.join(patches.hidden_files)}")
    else:
        log("  hidden tests: none (consider adding hidden.patch)")

    # The audit is cheap and its failures are the author's to fix; run it before
    # the image build so a forbidden identifier never waits on Docker.
    audit = audit_draft(draft, patches)
    for line in audit.format().splitlines():
        log(f"  {line}")
    if not audit.report.ok:
        raise AuthoringError(
            f"the report violates its tier ({draft.report_level}):\n  "
            + "\n  ".join(audit.report.violations)
        )

    spec = ImageSpec(
        repo=draft.repo,
        base_commit=draft.commit,
        python=draft.env.python,
        install=draft.env.install,
        bug_patch=patches.bug_patch,
    )
    log(f"  building image {spec.tag} ...")
    image = build_task_image(spec, cache_dir=cache_dir, rebuild=rebuild)
    log("  running tests: buggy + hidden, then fixed + hidden ...")
    derived = derive_tests(draft, patches, image=image, limits=limits)

    task = _build_task(
        draft,
        patches,
        audit,
        fail_to_pass=derived.fail_to_pass,
        pass_to_pass=derived.pass_to_pass,
    )
    path: Path | None = None
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(task.model_dump_json(indent=2) + "\n", encoding="utf-8")
        path = target
    return AuthoringResult(
        task=task, patches=patches, derived=derived, image=image, path=path, audit=audit
    )


def refresh_task(
    directory: Path,
    *,
    out_dir: Path,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> AuthoringResult:
    """Re-derive a shipped task's audit fields without Docker.

    Keeps the test lists the sandbox derived (``fail_to_pass`` / ``pass_to_pass``)
    and re-runs everything that needs only git and the tree: patch
    normalization, the audit, ``difficulty``.  Refuses when the source
    directory's patches no longer touch the files the shipped task's do -- that
    is a real change and needs ``make_task``.
    """
    draft = load_draft(directory)
    target = out_dir / f"{draft.id}.json"
    if not target.is_file():
        raise AuthoringError(f"{target} does not exist; build it with make_task first")
    from evals.benchmark.registry import load_task

    shipped = load_task(target)
    log(f"[{draft.id}] refreshing metadata against {draft.repo} @ {draft.commit[:12]}")
    patches = normalize(draft, cache_dir=cache_dir)
    if touched_files(patches.gold_patch) != touched_files(shipped.gold_patch):
        raise AuthoringError("the fix touches different files than the shipped task; run make_task")
    if (patches.hidden_files and set(patches.hidden_files) != set(shipped.hidden_test_files)) or (
        not patches.hidden_files and shipped.hidden_test_files
    ):
        raise AuthoringError("the hidden tests differ from the shipped task's; run make_task")
    if draft.test_command != shipped.test_command or draft.description != shipped.description:
        raise AuthoringError(
            "the test command or the description changed; the test lists must be re-derived "
            "with make_task"
        )
    audit = audit_draft(draft, patches)
    for line in audit.format().splitlines():
        log(f"  {line}")
    if not audit.report.ok:
        raise AuthoringError(
            f"the report violates its tier ({draft.report_level}):\n  "
            + "\n  ".join(audit.report.violations)
        )
    task = _build_task(
        draft,
        patches,
        audit,
        fail_to_pass=shipped.fail_to_pass,
        pass_to_pass=shipped.pass_to_pass,
    )
    # The sandbox-derived patches are the record; keep their exact text.
    task = task.model_copy(
        update={
            "bug_patch": shipped.bug_patch,
            "gold_patch": shipped.gold_patch,
            "hidden_test_patch": shipped.hidden_test_patch,
        }
    )
    path: Path | None = None
    if not dry_run:
        target.write_text(task.model_dump_json(indent=2) + "\n", encoding="utf-8")
        path = target
    return AuthoringResult(
        task=task, patches=patches, derived=None, image=None, path=path, audit=audit
    )


def report(result: AuthoringResult) -> str:
    task, derived = result.task, result.derived
    hidden = set(result.patches.hidden_files)
    lines = [f"task {task.id}: {task.category} / {task.difficulty} / {task.source} / {task.suite}"]
    if derived is not None:
        lines += [
            f"  test command (evaluation): {derived.command}",
            f"  buggy run: {derived.buggy_run.summary()}",
            f"  fixed run: {derived.fixed_run.summary()}",
        ]
    lines.append(f"  fail_to_pass ({len(task.fail_to_pass)}):")
    for nodeid in task.fail_to_pass:
        tag = "hidden " if _file_of(nodeid) in hidden else "visible"
        lines.append(f"    [{tag}] {nodeid}")
    lines.append(f"  pass_to_pass: {len(task.pass_to_pass)} test(s)")
    if derived is not None and derived.excluded:
        lines.append(f"  excluded ({len(derived.excluded)}, not passing in either run):")
        lines += [f"    {nodeid}: {why}" for nodeid, why in derived.excluded.items()]
    lines.append(f"  gold_files: {', '.join(task.gold_files)}")
    lines.append(f"  gold_symbols: {', '.join(task.gold_symbols) or '-'}")
    shape = task.shape
    lines.append(
        f"  report_level {task.report_level} · hidden_only {task.hidden_only} · cross_module "
        f"{task.cross_module} · shape {shape.label} ({shape.hunks} hunk(s), {shape.files} "
        f"file(s), -{shape.removed_lines}/+{shape.added_lines}) -> difficulty {task.difficulty}"
    )
    lines.append(f"  surface: {', '.join(task.surface_symbols) or '-'}")
    if result.path:
        lines.append(f"  written: {result.path}")
    else:
        lines.append("  dry run: nothing written")
    return "\n".join(lines)


# -- templates -------------------------------------------------------------------

TASK_TOML_TEMPLATE = """\
# RepoPilot-Bench task source. Build the task JSON with:
#   uv run python scripts/make_task.py {dir}
id = "{id}"
repo = "{repo}"
commit = "{commit}"            # full 40-char SHA the task is pinned to{real_block}
suite = "{suite}"                # v0 | v1
category = "{category}"       # off_by_one | wrong_condition | missing_check | state_management
                               # | retry_logic | cache_invalidation | exception_handling
                               # | propagation | ordering | other
report_level = "{report_level}"   # internal | public_api | symptom_only (bench-v1-design.md §6)
entry_points = []              # symptom_only: the (<= 3) names the report may use
test_command = "{test_command}"   # pytest command run from the repo root; keep it fast
# authored_difficulty = "medium"   # your estimate; the shipped difficulty is derived (§7)
# site_note = ""                    # why a site the site audit flagged was kept

[env]
python = "3.11"
install = "{install}"
test_timeout_seconds = 300

# Optional: override the AST-derived symbols, e.g. gold_symbols = ["LRUCache.__setitem__"]
"""

REAL_BLOCK = """
source = "real"                # the base commit is buggy; no bug.patch
fix_commit = "{fix_commit}"   # the upstream fix; commit above must be its parent
# hidden_pass_to_pass = true   # if the transplanted tests include guards that pass with the bug"""

DESCRIPTION_TEMPLATE = """\
<!-- The bug report the agent sees: what a user did, what happened, what was expected.
     Back-tick every identifier. public_api: name only documented public API, never the
     changed symbol or anything private; symptom_only: nothing below entry_points; no
     traceback frames from inside the package. Delete this comment. -->
"""


def init_draft(
    directory: Path,
    *,
    task_id: str,
    repo: str,
    commit: str,
    category: str = "other",
    test_command: str = "pytest tests",
    install: str = "pip install -e .",
    suite: str = "v1",
    report_level: str = "public_api",
    fix_commit: str | None = None,
) -> list[Path]:
    """Write template task.toml and description.md; patches are produced with git diff."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    toml_path = directory / "task.toml"
    if not toml_path.exists():
        toml_path.write_text(
            TASK_TOML_TEMPLATE.format(
                dir=directory,
                id=task_id,
                repo=repo,
                commit=commit,
                category=category,
                test_command=test_command,
                install=install,
                suite=suite,
                report_level=report_level,
                real_block=REAL_BLOCK.format(fix_commit=fix_commit) if fix_commit else "",
            ),
            encoding="utf-8",
        )
        written.append(toml_path)
    description_path = directory / "description.md"
    if not description_path.exists():
        description_path.write_text(DESCRIPTION_TEMPLATE, encoding="utf-8")
        written.append(description_path)
    return written
