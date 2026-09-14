"""Authoring RepoPilot-Bench tasks from a source directory (see scripts/make_task.py).

A task *source* directory holds only what a human has to write:

    task.toml        id, repo, commit, category, difficulty, test_command, [env]
    bug.patch        clean tree -> buggy tree: the injected mutation
    hidden.patch     adds regression test file(s); applied at evaluation only
    description.md   the bug report the agent sees

Everything else in the task JSON is derived by running the code, never typed:

* ``gold_patch`` -- the exact reverse of the mutation (``git diff -R``);
* ``gold_files`` / ``gold_symbols`` -- from the patch hunks and the AST of the
  buggy files (innermost function or class containing each changed line);
* ``fail_to_pass`` / ``pass_to_pass`` -- from two sandbox runs, buggy+hidden
  versus fixed+hidden, the way SWE-bench derives FAIL_TO_PASS / PASS_TO_PASS.

Packaging mistakes are rejected here instead of surfacing in a benchmark run:
a hidden test that passes with the bug, a gold patch that breaks an existing
test, a hidden patch that edits an existing file, a mutation that touches test
files, a test command that never collects the hidden tests.
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

from evals.benchmark.schema import (
    Category,
    Difficulty,
    Task,
    TaskEnv,
    TaskSource,
    looks_like_unified_diff,
    touched_files,
)
from evals.harness import with_hidden_files
from repopilot.sandbox.docker import ImageSpec, Sandbox, build_task_image
from repopilot.sandbox.limits import DEFAULT_LIMITS, SandboxLimits
from repopilot.sandbox.repo import DEFAULT_CACHE_DIR, export_tree
from repopilot.sandbox.results import TestOutcome, TestRun
from repopilot.tools.paths import is_test_path

SOURCES_DIR = Path(__file__).resolve().parent / "sources"
REQUIRED_FILES = ("task.toml", "bug.patch", "description.md")
HIDDEN_PATCH_FILE = "hidden.patch"
REQUIRED_KEYS = ("id", "repo", "commit", "category", "difficulty", "test_command")
OPTIONAL_KEYS = ("env", "gold_symbols")
GIT_IDENTITY = ("-c", "user.name=repopilot", "-c", "user.email=repopilot@localhost")

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
    difficulty: Difficulty
    test_command: str
    env: TaskEnv
    description: str
    bug_patch: str
    hidden_patch: str | None
    gold_symbols: tuple[str, ...] | None = None


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
        difficulty = Difficulty(meta["difficulty"])
    except (ValidationError, ValueError) as exc:
        raise AuthoringError(f"task.toml: {exc}") from exc

    description = (directory / "description.md").read_text(encoding="utf-8").strip()
    if not description:
        raise AuthoringError("description.md is empty")

    bug_patch = _read_patch(directory / "bug.patch")
    hidden_path = directory / HIDDEN_PATCH_FILE
    hidden_patch = _read_patch(hidden_path) if hidden_path.is_file() else None

    symbols = meta.get("gold_symbols")
    if symbols is not None and (
        not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols)
    ):
        raise AuthoringError("task.toml: gold_symbols must be a list of strings")

    return TaskDraft(
        directory=directory,
        id=str(meta["id"]),
        repo=str(meta["repo"]),
        commit=str(meta["commit"]),
        category=category,
        difficulty=difficulty,
        test_command=str(meta["test_command"]),
        env=env,
        description=description,
        bug_patch=bug_patch,
        hidden_patch=hidden_patch,
        gold_symbols=tuple(symbols) if symbols is not None else None,
    )


def _read_patch(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if not looks_like_unified_diff(text):
        raise AuthoringError(f"{path.name} is not a unified diff (needs '+++' and '@@' lines)")
    return text if text.endswith("\n") else text + "\n"


# -- patch normalization + localization targets ----------------------------------


@dataclass(frozen=True)
class NormalizedPatches:
    bug_patch: str
    gold_patch: str
    hidden_patch: str | None
    gold_files: tuple[str, ...]
    gold_symbols: tuple[str, ...]
    hidden_files: tuple[str, ...]


def normalize(draft: TaskDraft, *, cache_dir: Path = DEFAULT_CACHE_DIR) -> NormalizedPatches:
    """Re-express the human patches as git diffs and derive gold patch, files, symbols."""
    with tempfile.TemporaryDirectory(prefix="repopilot-author-") as tmp:
        work = Path(tmp) / "repo"
        export_tree(draft.repo, draft.commit, work, cache_dir=cache_dir)
        _git(work, "init", "-q", "-b", "main")
        _git(work, "add", "-A")
        _git(work, *GIT_IDENTITY, "commit", "-q", "--allow-empty", "-m", "clean")

        _apply(work, draft.bug_patch, "bug.patch")
        _git(work, *GIT_IDENTITY, "commit", "-q", "-m", "buggy")
        # Two commits, two plain diffs: clean -> buggy is the mutation, buggy -> clean the
        # fix.  (``git diff -R`` would swap the a/ b/ prefixes; this keeps them standard.)
        bug_patch = _git(work, "diff", "HEAD~1", "HEAD")
        gold_patch = _git(work, "diff", "HEAD", "HEAD~1")
        gold_files = tuple(sorted(touched_files(gold_patch)))
        if not gold_files:
            raise AuthoringError("bug.patch changes nothing")
        touched_tests = [f for f in gold_files if is_test_path(f)]
        if touched_tests:
            raise AuthoringError(
                f"bug.patch must only touch source files, not tests: {touched_tests}"
            )

        changed = changed_lines(gold_patch)  # old side of the gold patch == the buggy tree
        symbols: list[str] = []
        for path in gold_files:
            file = work / path
            if not file.is_file():
                continue  # the mutation deleted it
            try:
                found = enclosing_symbols(
                    file.read_text(encoding="utf-8"),
                    changed.modified.get(path, set()),
                    changed.inserted_before.get(path, set()),
                )
            except SyntaxError as exc:
                raise AuthoringError(f"bug.patch leaves {path} unparseable: {exc}") from exc
            symbols += [s for s in found if s not in symbols]

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
    )


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

    for hidden_file in patches.hidden_files:
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

    hidden_ids = [t for t in (*buggy.tests, *fixed.tests) if _file_of(t) in patches.hidden_files]
    undetecting = sorted(t for t in set(hidden_ids) if t not in fail_to_pass)
    if undetecting:
        detail = ", ".join(
            f"{t}: {buggy.outcome(t) or 'missing'} with bug, "
            f"{fixed.outcome(t) or 'missing'} with fix"
            for t in undetecting
        )
        raise AuthoringError(f"hidden tests must fail with the bug and pass with the fix: {detail}")
    if not fail_to_pass:
        raise AuthoringError("no test distinguishes the buggy tree from the fixed one")

    return DerivedTests(
        fail_to_pass=tuple(fail_to_pass),
        pass_to_pass=tuple(pass_to_pass),
        excluded=excluded,
        buggy_run=buggy,
        fixed_run=fixed,
        command=command,
    )


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


# -- orchestration ---------------------------------------------------------------


@dataclass(frozen=True)
class AuthoringResult:
    task: Task
    patches: NormalizedPatches
    derived: DerivedTests
    image: str
    path: Path | None


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

    try:
        task = Task(
            id=draft.id,
            repo=draft.repo,
            base_commit=draft.commit,
            source=TaskSource.MUTATION,
            description=draft.description,
            bug_patch=patches.bug_patch,
            gold_patch=patches.gold_patch,
            hidden_test_patch=patches.hidden_patch,
            fail_to_pass=list(derived.fail_to_pass),
            pass_to_pass=list(derived.pass_to_pass),
            test_command=draft.test_command,
            gold_files=list(patches.gold_files),
            gold_symbols=list(draft.gold_symbols or patches.gold_symbols),
            category=draft.category,
            difficulty=draft.difficulty,
            env=draft.env,
        )
    except ValidationError as exc:
        raise AuthoringError(f"derived task violates the schema:\n{exc}") from exc

    path: Path | None = None
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(task.model_dump_json(indent=2) + "\n", encoding="utf-8")
        path = target
    return AuthoringResult(task=task, patches=patches, derived=derived, image=image, path=path)


def report(result: AuthoringResult) -> str:
    task, derived = result.task, result.derived
    hidden = set(result.patches.hidden_files)
    lines = [
        f"task {task.id}: {task.category} / {task.difficulty} / {task.source}",
        f"  test command (evaluation): {derived.command}",
        f"  buggy run: {derived.buggy_run.summary()}",
        f"  fixed run: {derived.fixed_run.summary()}",
        f"  fail_to_pass ({len(task.fail_to_pass)}):",
    ]
    for nodeid in task.fail_to_pass:
        tag = "hidden " if _file_of(nodeid) in hidden else "visible"
        lines.append(f"    [{tag}] {nodeid}")
    lines.append(f"  pass_to_pass: {len(task.pass_to_pass)} test(s)")
    if derived.excluded:
        lines.append(f"  excluded ({len(derived.excluded)}, not passing in either run):")
        lines += [f"    {nodeid}: {why}" for nodeid, why in derived.excluded.items()]
    lines.append(f"  gold_files: {', '.join(task.gold_files)}")
    lines.append(f"  gold_symbols: {', '.join(task.gold_symbols) or '-'}")
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
commit = "{commit}"            # full 40-char SHA the task is pinned to
category = "{category}"       # off_by_one | wrong_condition | missing_check | state_management
                               # | retry_logic | cache_invalidation | exception_handling | other
difficulty = "{difficulty}"   # easy | medium | hard
test_command = "{test_command}"   # pytest command run from the repo root; keep it fast

[env]
python = "3.11"
install = "{install}"
test_timeout_seconds = 300

# Optional: override the AST-derived symbols, e.g. gold_symbols = ["LRUCache.__setitem__"]
"""

DESCRIPTION_TEMPLATE = """\
<!-- The bug report the agent sees. Describe observable behaviour and how to reproduce
     it; do not name the file, the function (medium/hard) or the fix. Delete this comment. -->
"""


def init_draft(
    directory: Path,
    *,
    task_id: str,
    repo: str,
    commit: str,
    category: str = "other",
    difficulty: str = "medium",
    test_command: str = "pytest tests",
    install: str = "pip install -e .",
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
                difficulty=difficulty,
                test_command=test_command,
                install=install,
            ),
            encoding="utf-8",
        )
        written.append(toml_path)
    description_path = directory / "description.md"
    if not description_path.exists():
        description_path.write_text(DESCRIPTION_TEMPLATE, encoding="utf-8")
        written.append(description_path)
    return written
