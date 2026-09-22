"""Materializing a repository at a pinned commit, host side, for image builds.

Remote repositories are mirrored once under the cache directory and refreshed
only when a requested commit is missing, so repeated image builds never
re-download.  Local paths are used directly.

The exported tree carries no ``.git`` directory: the container gets a fresh,
single-commit history (see ``docker.py``), so neither the upstream fix commit
nor the injected bug can be discovered through git history.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

DEFAULT_CACHE_DIR = Path(os.environ.get("REPOPILOT_CACHE_DIR", "~/.cache/repopilot")).expanduser()


class RepoError(RuntimeError):
    """git failed or the requested commit does not exist."""


def _git(*args: str, timeout: float = 600) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise RepoError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RepoError(f"git {' '.join(args[:3])} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise RepoError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def is_local_repo(repo: str) -> bool:
    return Path(repo).expanduser().is_dir()


def has_commit(git_dir: Path, commit: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(git_dir), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def mirror_path(repo: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    digest = hashlib.sha256(repo.encode("utf-8")).hexdigest()[:16]
    return cache_dir / "repos" / f"{digest}.git"


def ensure_mirror(repo: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    """Bare mirror of a remote repository, cloned on first use."""
    path = mirror_path(repo, cache_dir)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "--mirror", "--quiet", repo, str(path))
    return path


def resolve_source(repo: str, commit: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    """A local git directory that contains ``commit``: the repo itself, or its mirror."""
    if is_local_repo(repo):
        source = Path(repo).expanduser().resolve()
        if not has_commit(source, commit):
            raise RepoError(f"commit {commit} not found in local repository {source}")
        return source
    mirror = ensure_mirror(repo, cache_dir)
    if not has_commit(mirror, commit):
        _git("-C", str(mirror), "remote", "update", "--prune")
        if not has_commit(mirror, commit):
            raise RepoError(f"commit {commit} not found in {repo} (mirror refreshed)")
    return mirror


def export_tree(repo: str, commit: str, dest: Path, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    """Write the working tree of ``repo`` at ``commit`` into ``dest`` (no ``.git``)."""
    dest = Path(dest)
    if dest.exists():
        raise RepoError(f"export destination already exists: {dest}")
    source = resolve_source(repo, commit, cache_dir)
    _git("clone", "--quiet", "--no-checkout", "--shared", str(source), str(dest))
    _git("-C", str(dest), "checkout", "--quiet", "--detach", commit)
    shutil.rmtree(dest / ".git")
    return dest


def parent_commit(repo: str, commit: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> str:
    """The first parent of ``commit`` (a real task's base is its fix's parent)."""
    source = resolve_source(repo, commit, cache_dir)
    parents = _git("-C", str(source), "rev-list", "--parents", "-n", "1", commit).split()
    if len(parents) < 2:
        raise RepoError(f"commit {commit} has no parent")
    if len(parents) > 2:
        raise RepoError(f"commit {commit} is a merge; real tasks need a single-parent fix commit")
    return parents[1]


def diff_between(
    repo: str,
    base: str,
    target: str,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    paths: tuple[str, ...] = (),
) -> str:
    """``git diff base target [-- paths]`` from the repository's history."""
    source = resolve_source(repo, target, cache_dir)
    if not has_commit(source, base):
        raise RepoError(f"commit {base} not found in {repo}")
    args = ["-C", str(source), "diff", "--no-color", base, target]
    if paths:
        args += ["--", *paths]
    return _git(*args)


def changed_paths(
    repo: str, base: str, target: str, cache_dir: Path = DEFAULT_CACHE_DIR
) -> list[str]:
    """Paths that differ between two commits."""
    source = resolve_source(repo, target, cache_dir)
    out = _git("-C", str(source), "diff", "--name-only", base, target)
    return [line for line in out.splitlines() if line.strip()]
