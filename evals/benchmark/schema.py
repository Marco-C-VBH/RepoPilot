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
    OTHER = "other"


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


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
    difficulty: Difficulty
    env: TaskEnv = Field(default_factory=TaskEnv)

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

    @field_validator("gold_files")
    @classmethod
    def _gold_files_are_repo_relative(cls, value: list[str]) -> list[str]:
        _check_unique(value, "gold_files")
        return [_check_repo_relative_path(p) for p in value]

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


def task_json_schema() -> dict[str, Any]:
    """JSON Schema for task files (exported to evals/benchmark/task.schema.json)."""
    schema = Task.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "RepoPilot-Bench task"
    return schema
