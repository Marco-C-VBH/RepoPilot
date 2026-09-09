"""Helpers to build small git repositories for tests (host side, no Docker)."""

from __future__ import annotations

import subprocess
from pathlib import Path

GIT_ENV_ARGS = ["-c", "user.name=tests", "-c", "user.email=tests@localhost"]


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *GIT_ENV_ARGS, *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def init_repo(path: Path, files: dict[str, str], message: str = "initial") -> str:
    """Create a git repository at ``path`` with ``files`` committed; returns the commit SHA."""
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=path)
    return commit_all(path, files, message)


def commit_all(path: Path, files: dict[str, str], message: str) -> str:
    """Write ``files`` into an existing repository and commit everything; returns the SHA."""
    write_files(path, files)
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", message, cwd=path)
    return git("rev-parse", "HEAD", cwd=path)
