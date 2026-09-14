"""Path rules shared by the tools, the harness and the task authoring pipeline."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

TEXT_SUFFIXES = {
    ".py",
    ".pyi",
    ".txt",
    ".md",
    ".rst",
    ".toml",
    ".cfg",
    ".ini",
    ".yaml",
    ".yml",
    ".json",
    ".in",
    ".typed",
}


class PathError(ValueError):
    """A path the tools refuse: absolute, escaping the repository, or inside .git."""


def is_test_path(path: str) -> bool:
    """Whether ``path`` belongs to a test suite (directory or file naming convention)."""
    parts = Path(path).parts
    name = parts[-1]
    return (
        "tests" in parts[:-1]
        or "test" in parts[:-1]
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def is_text_path(path: str) -> bool:
    """Files the search tools look inside (source, docs, config); everything else is skipped."""
    p = PurePosixPath(path)
    return p.suffix in TEXT_SUFFIXES or p.name in {"Makefile", "LICENSE", "Dockerfile"}


def resolve_repo_path(root: Path, path: str) -> Path:
    """Turn a repo-relative path from the model into an absolute path inside ``root``.

    Rejects empty or absolute paths, ``..`` segments, anything resolving outside
    the repository (symlinks included) and the ``.git`` directory.
    """
    if not isinstance(path, str) or not path.strip() or path.strip() != path:
        raise PathError("path must be a non-empty repository-relative path")
    pure = PurePosixPath(path)
    if pure.is_absolute() or path.startswith("~"):
        raise PathError(f"{path!r} is not repository-relative")
    if ".." in pure.parts:
        raise PathError(f"{path!r} must not contain '..'")
    root_resolved = root.resolve()
    candidate = (root_resolved / pure).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise PathError(f"{path!r} is outside the repository")
    relative = candidate.relative_to(root_resolved)
    if relative.parts and relative.parts[0] == ".git":
        raise PathError("the .git directory is off limits")
    return candidate
