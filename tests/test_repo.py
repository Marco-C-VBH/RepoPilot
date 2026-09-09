"""Host-side repository export (repopilot/sandbox/repo.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from repopilot.sandbox.repo import RepoError, export_tree, mirror_path, resolve_source
from tests.gitfixtures import commit_all, init_repo


def test_export_local_repo_at_older_commit(tmp_path: Path) -> None:
    src = tmp_path / "src"
    first = init_repo(src, {"a.txt": "one\n", "pkg/__init__.py": ""})
    commit_all(src, {"a.txt": "two\n"}, "second")

    dest = export_tree(str(src), first, tmp_path / "out", cache_dir=tmp_path / "cache")

    assert (dest / "a.txt").read_text() == "one\n"
    assert (dest / "pkg" / "__init__.py").exists()
    assert not (dest / ".git").exists(), "exported tree must carry no history"
    assert not (tmp_path / "cache").exists(), "local repos are used directly, not mirrored"


def test_unknown_commit_raises(tmp_path: Path) -> None:
    src = tmp_path / "src"
    init_repo(src, {"a.txt": "one\n"})
    with pytest.raises(RepoError, match="not found"):
        export_tree(str(src), "0" * 40, tmp_path / "out", cache_dir=tmp_path / "cache")


def test_existing_destination_is_refused(tmp_path: Path) -> None:
    src = tmp_path / "src"
    sha = init_repo(src, {"a.txt": "one\n"})
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(RepoError, match="already exists"):
        export_tree(str(src), sha, dest, cache_dir=tmp_path / "cache")


def test_remote_repos_are_mirrored_once_and_refreshed_on_demand(tmp_path: Path) -> None:
    src = tmp_path / "src"
    first = init_repo(src, {"a.txt": "one\n"})
    url = src.as_uri()  # file:// URL is "remote" as far as the mirror cache is concerned
    cache = tmp_path / "cache"

    export_tree(url, first, tmp_path / "out1", cache_dir=cache)
    mirror = mirror_path(url, cache)
    assert mirror.is_dir()
    assert resolve_source(url, first, cache) == mirror

    # A commit made after the mirror was created triggers exactly one refresh.
    second = commit_all(src, {"a.txt": "two\n"}, "second")
    dest = export_tree(url, second, tmp_path / "out2", cache_dir=cache)
    assert (dest / "a.txt").read_text() == "two\n"

    with pytest.raises(RepoError, match="mirror refreshed"):
        export_tree(url, "f" * 40, tmp_path / "out3", cache_dir=cache)


def test_mirror_path_is_stable_per_url(tmp_path: Path) -> None:
    a = mirror_path("https://github.com/example/a", tmp_path)
    assert a == mirror_path("https://github.com/example/a", tmp_path)
    assert a != mirror_path("https://github.com/example/b", tmp_path)
    assert a.suffix == ".git"
