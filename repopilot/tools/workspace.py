"""The agent's working copy: a host-side git checkout of the buggy tree.

Reading, searching and editing happen here, on the host, without a container
round trip per call.  Only test runs go to the sandbox: the workspace's diff is
applied to a fresh copy of the same tree inside the container, so the tests see
exactly what the agent edited and nothing the agent did on the host can
execute anywhere.  The same diff, taken at the end, is the candidate patch the
harness judges.

``HEAD`` is the buggy commit (base commit plus ``bug_patch``), mirroring the
task image, so ``diff()`` is always "what the agent changed".
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from repopilot.sandbox.repo import DEFAULT_CACHE_DIR, export_tree
from repopilot.tools.paths import PathError, resolve_repo_path

GIT_IDENTITY = ["-c", "user.name=repopilot", "-c", "user.email=repopilot@localhost"]
_BINARY_PROBE = 8192


class WorkspaceError(RuntimeError):
    pass


def _git(cwd: Path, *args: str, stdin: str | None = None, timeout: float = 300) -> str:
    proc = subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()}")
    return proc.stdout


class Workspace:
    """A git working copy the tools operate on.  Use as a context manager."""

    def __init__(self, root: Path, *, tempdir: Path | None = None) -> None:
        self.root = Path(root)
        self._tempdir = tempdir
        self.version = 0  # bumped on every change; indexes use it to notice edits
        self._files: list[str] | None = None

    @classmethod
    def create(
        cls,
        repo: str,
        base_commit: str,
        bug_patch: str | None = None,
        *,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        parent: Path | None = None,
    ) -> Workspace:
        """Export ``repo`` at ``base_commit`` into a temp dir and commit it (plus the bug)."""
        tempdir = Path(tempfile.mkdtemp(prefix="repopilot-ws-", dir=parent))
        try:
            root = tempdir / "repo"
            export_tree(repo, base_commit, root, cache_dir=cache_dir)
            _git(root, "init", "-q", "-b", "main")
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "--allow-empty", "-m", "base")
            if bug_patch:
                _git(
                    root, "apply", "--index", "--whitespace=nowarn", "-", stdin=_newline(bug_patch)
                )
                _git(root, "commit", "-q", "-m", "bug")
        except Exception:
            shutil.rmtree(tempdir, ignore_errors=True)
            raise
        return cls(root, tempdir=tempdir)

    @classmethod
    def from_repo(cls, root: Path) -> Workspace:
        """Wrap an existing git checkout (its HEAD is treated as the buggy tree)."""
        root = Path(root)
        if not (root / ".git").exists():
            raise WorkspaceError(f"{root} is not a git repository")
        return cls(root)

    # -- lifecycle --------------------------------------------------------------------
    def __enter__(self) -> Workspace:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._tempdir is not None:
            shutil.rmtree(self._tempdir, ignore_errors=True)
            self._tempdir = None

    # -- files ------------------------------------------------------------------------
    def resolve(self, path: str) -> Path:
        return resolve_repo_path(self.root, path)

    def files(self) -> list[str]:
        """Repo-relative paths of tracked and untracked-but-not-ignored files, sorted."""
        if self._files is None:
            out = _git(self.root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
            self._files = sorted({p for p in out.split("\0") if p})
        return list(self._files)

    def exists(self, path: str) -> bool:
        try:
            return self.resolve(path).is_file()
        except PathError:
            return False

    def read_text(self, path: str) -> str:
        file = self.resolve(path)
        if not file.is_file():
            raise PathError(f"{path!r} does not exist")
        data = file.read_bytes()
        if b"\0" in data[:_BINARY_PROBE]:
            raise PathError(f"{path!r} is a binary file")
        return data.decode("utf-8", errors="replace")

    def write_text(self, path: str, text: str) -> None:
        file = self.resolve(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
        self.version += 1
        self._files = None

    # -- git --------------------------------------------------------------------------
    def diff(self) -> str:
        """Unified diff of the working copy against the buggy commit (new files included)."""
        _git(self.root, "add", "--intent-to-add", "--all")
        return _git(self.root, "diff", "HEAD")

    def changed_files(self) -> list[str]:
        _git(self.root, "add", "--intent-to-add", "--all")
        out = _git(self.root, "diff", "HEAD", "--name-only")
        return sorted(line for line in out.splitlines() if line)

    def reset(self) -> None:
        """Discard every change: back to the buggy tree."""
        _git(self.root, "reset", "-q", "--hard", "HEAD")
        _git(self.root, "clean", "-fdq")
        self.version += 1
        self._files = None


def _newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"
