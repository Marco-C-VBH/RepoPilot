"""Task schema for RepoPilot-Bench (spec §10.2 - §10.4).

A task is one reproducible repository-level debugging problem:

* a repository pinned to a full commit SHA,
* the bug report the agent sees (``description``),
* the reference fix (``gold_patch``) and its localization targets
  (``gold_files`` / ``gold_symbols``),
* the tests that decide success.

Success is deterministic (spec §10.4).  After the candidate patch is applied and
``hidden_test_patch`` is layered on top, every ``fail_to_pass`` test must pass
*and* every ``pass_to_pass`` test must still pass.  No LLM judge.

Two task sources (spec §10.2):

* ``real``      -- ``base_commit`` already contains the bug; ``bug_patch`` is absent.
* ``mutation``  -- ``base_commit`` is clean; the harness applies ``bug_patch`` to
  inject the bug before the agent ever sees the repository.

Harness invariants enforced here rather than discovered at run time:

* every patch is a unified diff with at least one hunk,
* every ``gold_files`` entry is actually modified by ``gold_patch``,
* ``fail_to_pass`` and ``pass_to_pass`` do not overlap,
* unknown fields are rejected, so typos in task files fail loudly.

Bench v1 (docs/bench-v1-design.md §7) adds the fields that make a task's
difficulty a matter of record instead of judgement: which ``suite`` it belongs
to, at what ``report_level`` the bug report is written (and, for symptom-only
reports, the ``entry_points`` it may name), which repository symbols and files
the report actually names (``surface_symbols`` / ``surface_files``, derived by
the report audit against the tree the agent sees), whether the fix lives
somewhere the report does not point at (``cross_module``), and the upstream
``fix_commit`` a real task was made from.  ``hidden_only``, ``shape`` and
``derived_difficulty`` are computed from the other fields; ``difficulty`` must
equal the derived value once a task has been audited.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ID_PATTERN = r"^[a-z0-9]+(?:_[a-z0-9]+)*$"
COMMIT_PATTERN = r"^[0-9a-f]{40}$"
PYTHON_VERSION_PATTERN = r"^3\.\d{1,2}$"

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.MULTILINE)
_DIFF_GIT_RE = re.compile(r"^diff --git (\S+) (\S+)$", re.MULTILINE)
_OLD_FILE_RE = re.compile(r"^--- (\S+)", re.MULTILINE)
_NEW_FILE_RE = re.compile(r"^\+\+\+ (\S+)", re.MULTILINE)
_PREFIX_RE = re.compile(r"^[ab]/")


class TaskSource(StrEnum):
    """Where the bug comes from (spec §10.2)."""

    REAL = "real"
    MUTATION = "mutation"


class Category(StrEnum):
    """Fault category; drives the failure-analysis breakdown (spec §10.2, §11.2)."""

    OFF_BY_ONE = "off_by_one"
    WRONG_CONDITION = "wrong_condition"
    MISSING_CHECK = "missing_check"
    STATE_MANAGEMENT = "state_management"
    RETRY_LOGIC = "retry_logic"
    CACHE_INVALIDATION = "cache_invalidation"
    EXCEPTION_HANDLING = "exception_handling"
    PROPAGATION = "propagation"  # v1: a value or flag accepted at one layer, lost before the next
    ORDERING = "ordering"  # v1: wrong or unstable order, precedence, or pass sequence
    OTHER = "other"


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class Suite(StrEnum):
    """Which benchmark generation added the task (docs/bench-v1-design.md §2)."""

    V0 = "v0"
    V1 = "v1"


class ReportLevel(StrEnum):
    """How much the bug report gives away (docs/bench-v1-design.md §6.1)."""

    INTERNAL = "internal"  # names the changed symbol (v0)
    PUBLIC_API = "public_api"  # names only documented public API a user would type
    SYMPTOM_ONLY = "symptom_only"  # names nothing below the task's entry points


class Shape(StrEnum):
    """The reference fix's footprint, derived from ``gold_patch``."""

    SINGLE_LINE = "single_line"  # at most one removed and one added line
    MULTI_LINE = "multi_line"  # one hunk, several lines
    MULTI_SITE = "multi_site"  # two or more hunks in one file
    CROSS_FILE = "cross_file"  # hunks in two or more files


def looks_like_unified_diff(text: str) -> bool:
    """True when ``text`` has a ``+++`` file header and at least one ``@@`` hunk."""
    return bool(_HUNK_RE.search(text)) and bool(_NEW_FILE_RE.search(text))


def touched_files(patch: str) -> frozenset[str]:
    """Repository-relative paths a unified diff creates, modifies or deletes.

    Prefers ``diff --git <old> <new>`` headers (git format, including the
    swapped ``b/ a/`` prefixes of ``git diff -R`` and ``--no-prefix`` output);
    falls back to the ``---``/``+++`` file headers for plain ``diff -u``
    output.  ``/dev/null`` (file creation / deletion) is never reported.
    """
    git_headers = _DIFF_GIT_RE.findall(patch)
    if git_headers:
        paths: set[str] = set()
        for old, new in git_headers:
            if old == new:  # --no-prefix output: the two sides are the bare path
                paths.add(old)
            else:
                paths.update(_strip_prefix(old, new))
        return frozenset(paths)
    paths = {m.group(1) for regex in (_OLD_FILE_RE, _NEW_FILE_RE) for m in regex.finditer(patch)}
    paths.discard("/dev/null")
    return frozenset(_PREFIX_RE.sub("", p) for p in paths)


def _strip_prefix(old: str, new: str) -> tuple[str, str]:
    """Drop git's one-component ``a/``/``b/`` (or swapped ``b/``/``a/``) prefixes."""
    old_prefix, _, old_rest = old.partition("/")
    new_prefix, _, new_rest = new.partition("/")
    if old_rest and new_rest and {old_prefix, new_prefix} == {"a", "b"}:
        return old_rest, new_rest
    return old, new


class PatchShape(BaseModel):
    """Hunks, files and changed lines of a unified diff, and the ``Shape`` they imply."""

    model_config = ConfigDict(frozen=True)

    hunks: int
    files: int
    removed_lines: int
    added_lines: int

    @property
    def changed_lines(self) -> int:
        return self.removed_lines + self.added_lines

    @property
    def label(self) -> Shape:
        if self.files >= 2:
            return Shape.CROSS_FILE
        if self.hunks >= 2:
            return Shape.MULTI_SITE
        if self.removed_lines <= 1 and self.added_lines <= 1:
            return Shape.SINGLE_LINE
        return Shape.MULTI_LINE

    @property
    def multi_site(self) -> bool:
        return self.label in (Shape.MULTI_SITE, Shape.CROSS_FILE)


_CHANGE_SKIP = ("+++ ", "--- ")


def patch_shape(patch: str) -> PatchShape:
    """Count the hunks, files and changed lines of a unified diff."""
    removed = added = 0
    for line in patch.splitlines():
        if line.startswith(_CHANGE_SKIP):
            continue
        if line.startswith("-"):
            removed += 1
        elif line.startswith("+"):
            added += 1
    return PatchShape(
        hunks=len(_HUNK_RE.findall(patch)),
        files=len(touched_files(patch)),
        removed_lines=removed,
        added_lines=added,
    )


def _check_repo_relative_path(path: str) -> str:
    # Split by hand: PurePosixPath would silently normalize away "./" and "//".
    segments = path.split("/")
    if (
        not path
        or path != path.strip()
        or "\\" in path
        or any(segment in ("", ".", "..") for segment in segments)
    ):
        raise ValueError(f"{path!r} must be a normalized relative POSIX path inside the repository")
    return path


def _check_unique(values: list[str], what: str) -> list[str]:
    if any(not v or v != v.strip() for v in values):
        raise ValueError(f"{what} entries must be non-empty and have no surrounding whitespace")
    if len(set(values)) != len(values):
        raise ValueError(f"{what} contains duplicate entries")
    return values


class TaskEnv(BaseModel):
    """How the per-task sandbox image is built (spec §9.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    python: str = Field(
        default="3.11",
        pattern=PYTHON_VERSION_PATTERN,
        description="Python minor version of the sandbox image, e.g. '3.11'.",
    )
    install: str = Field(
        default="pip install -e .",
        min_length=1,
        description=(
            "Shell command run once at image build time, from the repository root, with "
            "network access. Must install the project and its test dependencies."
        ),
    )
    test_timeout_seconds: int = Field(
        default=300,
        ge=1,
        le=3600,
        description="Wall-clock limit for a single test run inside the sandbox.",
    )


class Task(BaseModel):
    """One RepoPilot-Bench task (spec §10.3, extended for deterministic validation)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(
        pattern=ID_PATTERN,
        description="Unique task id, lower_snake_case; the task file must be named '<id>.json'.",
        examples=["cachetools_001"],
    )
    repo: str = Field(
        min_length=1,
        description="Git URL (or absolute local path) of the repository to clone.",
    )
    base_commit: str = Field(
        pattern=COMMIT_PATTERN,
        description="Full 40-character commit SHA the sandbox checks out. Short SHAs are rejected.",
    )
    source: TaskSource = Field(
        description="'real' (bug present at base_commit) or 'mutation' (bug injected by bug_patch)."
    )
    description: str = Field(
        min_length=1,
        description="The natural-language bug report shown to the agent. Must not leak the fix.",
    )
    bug_patch: str | None = Field(
        default=None,
        description=(
            "Mutation tasks only: unified diff applied to the clean base_commit at image build "
            "time to inject the bug. Must be absent for 'real' tasks."
        ),
    )
    gold_patch: str = Field(
        description=(
            "Reference fix as a unified diff. Used by the 'gold' oracle solver to validate the "
            "harness and by failure analysis; never shown to the agent."
        ),
    )
    hidden_test_patch: str | None = Field(
        default=None,
        description=(
            "Unified diff adding/changing regression tests, applied only at evaluation time so "
            "the agent cannot read the hidden tests from the repository."
        ),
    )
    fail_to_pass: list[str] = Field(
        min_length=1,
        description="pytest node ids that fail before the fix and must pass after it.",
        examples=[["tests/test_retry.py::test_retry_is_idempotent"]],
    )
    pass_to_pass: list[str] = Field(
        default_factory=list,
        description=(
            "pytest node ids that pass before the fix and must still pass (regression guard)."
        ),
    )
    test_command: str = Field(
        min_length=1,
        description=(
            "Base pytest command run from the repository root, e.g. 'pytest tests/test_retry.py'. "
            "The harness appends its own --junitxml flag."
        ),
    )
    gold_files: list[str] = Field(
        min_length=1,
        description=(
            "Repository-relative files the fix must touch; the localization target (Recall@k)."
        ),
    )
    gold_symbols: list[str] = Field(
        default_factory=list,
        description="Functions/classes/methods the fix touches, e.g. 'retry_request'.",
    )
    category: Category
    difficulty: Difficulty = Field(
        description=(
            "easy / medium / hard. Derived (docs/bench-v1-design.md §7): one point each for "
            "cross_module, hidden_only, a multi-site or cross-file fix, and a symptom_only "
            "report; 0 -> easy, 1 -> medium, 2+ -> hard. Must equal derived_difficulty once "
            "the report audit has filled surface_files."
        )
    )
    env: TaskEnv = Field(default_factory=TaskEnv)
    suite: Suite = Field(
        default=Suite.V0, description="Benchmark generation that added the task: v0 or v1."
    )
    report_level: ReportLevel = Field(
        default=ReportLevel.INTERNAL,
        description=(
            "What the report may name: internal (the changed symbol; v0), public_api "
            "(documented public API only), symptom_only (nothing below entry_points)."
        ),
    )
    entry_points: list[str] = Field(
        default_factory=list,
        description=(
            "symptom_only reports: the (at most three) qualified names the report may use, "
            "e.g. 'sqlparse.format' or 'Console.print'."
        ),
    )
    surface_symbols: list[str] = Field(
        default_factory=list,
        description=(
            "Repository symbols the report names, resolved by the audit against the tree the "
            "agent sees (qualified names). Derived; empty until a task has been audited."
        ),
    )
    surface_files: list[str] = Field(
        default_factory=list,
        description=(
            "Files defining surface_symbols (the entry points' files when the report names "
            "nothing). Derived. The localization 'start' a report offers."
        ),
    )
    cross_module: bool = Field(
        default=False,
        description=(
            "Derived: no surface file is a gold file, so the fix lives somewhere the report "
            "does not point at. False (unknown) until audited."
        ),
    )
    fix_commit: str | None = Field(
        default=None,
        pattern=COMMIT_PATTERN,
        description=(
            "real tasks: the upstream commit the fix was taken from (base_commit is its "
            "parent). Provenance only; never shown to the agent."
        ),
    )
    hidden_pass_to_pass: bool = Field(
        default=False,
        description=(
            "The hidden test file also carries regression guards that pass with the bug "
            "(typically the upstream tests transplanted with a real fix); they are listed "
            "in pass_to_pass on purpose. False means every hidden test is fail_to_pass."
        ),
    )

    # -- field validators -------------------------------------------------

    @field_validator("bug_patch", "gold_patch", "hidden_test_patch")
    @classmethod
    def _patches_are_unified_diffs(cls, value: str | None) -> str | None:
        if value is not None and not looks_like_unified_diff(value):
            raise ValueError(
                "must be a unified diff with a '+++' header and at least one '@@' hunk"
            )
        return value

    @field_validator("fail_to_pass", "pass_to_pass")
    @classmethod
    def _test_ids_are_unique(cls, value: list[str]) -> list[str]:
        return _check_unique(value, "test ids")

    @field_validator("gold_symbols")
    @classmethod
    def _symbols_are_unique(cls, value: list[str]) -> list[str]:
        return _check_unique(value, "gold_symbols")

    @field_validator("gold_files", "surface_files")
    @classmethod
    def _files_are_repo_relative(cls, value: list[str]) -> list[str]:
        _check_unique(value, "file lists")
        return [_check_repo_relative_path(p) for p in value]

    @field_validator("entry_points", "surface_symbols")
    @classmethod
    def _names_are_unique(cls, value: list[str]) -> list[str]:
        return _check_unique(value, "name lists")

    # -- cross-field validators -------------------------------------------

    @model_validator(mode="after")
    def _source_matches_bug_patch(self) -> Task:
        if self.source is TaskSource.MUTATION and self.bug_patch is None:
            raise ValueError("mutation tasks require bug_patch")
        if self.source is TaskSource.REAL and self.bug_patch is not None:
            raise ValueError(
                "real tasks must not have bug_patch (the bug is already at base_commit)"
            )
        return self

    @model_validator(mode="after")
    def _test_sets_are_disjoint(self) -> Task:
        overlap = sorted(set(self.fail_to_pass) & set(self.pass_to_pass))
        if overlap:
            raise ValueError(f"tests listed in both fail_to_pass and pass_to_pass: {overlap}")
        return self

    @model_validator(mode="after")
    def _gold_files_are_touched_by_gold_patch(self) -> Task:
        touched = touched_files(self.gold_patch)
        missing = [f for f in self.gold_files if f not in touched]
        if missing:
            raise ValueError(f"gold_files not modified by gold_patch: {missing}")
        return self

    @model_validator(mode="after")
    def _report_level_and_entry_points_agree(self) -> Task:
        if self.report_level is ReportLevel.SYMPTOM_ONLY and not self.entry_points:
            raise ValueError("symptom_only reports need at least one entry point")
        if len(self.entry_points) > 3:
            raise ValueError("at most three entry points (docs/bench-v1-design.md §6.1)")
        return self

    @model_validator(mode="after")
    def _fix_commit_only_for_real_tasks(self) -> Task:
        if self.fix_commit is not None and self.source is not TaskSource.REAL:
            raise ValueError("fix_commit is for real tasks (source == 'real')")
        if self.fix_commit is not None and self.fix_commit == self.base_commit:
            raise ValueError("fix_commit must differ from base_commit (its parent)")
        return self

    @model_validator(mode="after")
    def _cross_module_matches_surface(self) -> Task:
        if self.surface_files:
            expected = not (set(self.surface_files) & set(self.gold_files))
            if self.cross_module != expected:
                raise ValueError(
                    f"cross_module must be {expected}: surface_files {self.surface_files} "
                    f"{'do not meet' if expected else 'meet'} gold_files {self.gold_files}"
                )
        elif self.cross_module:
            raise ValueError("cross_module needs surface_files (run the report audit)")
        return self

    # -- derived views ----------------------------------------------------

    @property
    def is_mutation(self) -> bool:
        return self.source is TaskSource.MUTATION

    @property
    def gold_patch_files(self) -> frozenset[str]:
        """Every file the reference fix touches (a superset of ``gold_files``)."""
        return touched_files(self.gold_patch)

    @property
    def hidden_test_files(self) -> tuple[str, ...]:
        """Files added by ``hidden_test_patch`` (sorted); empty when there is none."""
        if not self.hidden_test_patch:
            return ()
        return tuple(sorted(touched_files(self.hidden_test_patch)))

    @property
    def all_tests(self) -> list[str]:
        return [*self.fail_to_pass, *self.pass_to_pass]

    @property
    def repo_name(self) -> str:
        """The repository's short name (``.../pallets/click`` -> ``click``)."""
        return self.repo.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")

    @property
    def hidden_only(self) -> bool:
        """True when only the hidden tests catch the bug (the visible suite stays green)."""
        hidden = set(self.hidden_test_files)
        if not hidden:
            return False
        return all(nodeid.split("::", 1)[0] in hidden for nodeid in self.fail_to_pass)

    @property
    def hidden_guards(self) -> tuple[str, ...]:
        """Hidden-file tests shipped in pass_to_pass (allowed only with hidden_pass_to_pass)."""
        hidden = set(self.hidden_test_files)
        return tuple(t for t in self.pass_to_pass if t.split("::", 1)[0] in hidden)

    @property
    def shape(self) -> PatchShape:
        return patch_shape(self.gold_patch)

    @property
    def derived_difficulty(self) -> Difficulty:
        """docs/bench-v1-design.md §7: one point per hardness factor."""
        return derive_difficulty(
            cross_module=self.cross_module,
            hidden_only=self.hidden_only,
            multi_site=self.shape.multi_site,
            symptom_only=self.report_level is ReportLevel.SYMPTOM_ONLY,
        )

    @property
    def audited(self) -> bool:
        """Whether the report audit has filled the derived fields."""
        return bool(self.surface_files)

    def flags(self) -> dict[str, Any]:
        """The breakdown keys a results table can group by."""
        return {
            "suite": str(self.suite),
            "repo": self.repo_name,
            "source": str(self.source),
            "report_level": str(self.report_level),
            "hidden_only": self.hidden_only,
            "cross_module": self.cross_module,
            "shape": str(self.shape.label),
            "category": str(self.category),
            "difficulty": str(self.difficulty),
        }


def derive_difficulty(
    *, cross_module: bool, hidden_only: bool, multi_site: bool, symptom_only: bool
) -> Difficulty:
    """One point per hardness factor: 0 -> easy, 1 -> medium, 2+ -> hard (§7)."""
    points = int(cross_module) + int(hidden_only) + int(multi_site) + int(symptom_only)
    if points == 0:
        return Difficulty.EASY
    if points == 1:
        return Difficulty.MEDIUM
    return Difficulty.HARD


def task_json_schema() -> dict[str, Any]:
    """JSON Schema for task files (exported to evals/benchmark/task.schema.json)."""
    schema = Task.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "RepoPilot-Bench task"
    return schema
