"""Guard: no API key can be committed, and .env stays ignored.

Scans every file git would track (the working tree minus ignored directories)
for the vendors' key prefixes.  It runs in CI on every push, so a key pasted
into a script or a log file fails the build before it reaches GitHub -- and if
one ever does get through, revoke it in the provider console first, then fix
the history.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "work", "results", "runs", ".repopilot", "__pycache__"}
KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),  # Anthropic
    re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}"),  # OpenAI
]
TEXT_SUFFIXES = {
    ".py",
    ".toml",
    ".md",
    ".json",
    ".jsonl",
    ".yml",
    ".yaml",
    ".txt",
    ".cfg",
    ".patch",
    ".sh",
}


def tracked_text_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        if path.name.startswith(".env"):
            continue  # the local .env is allowed to hold keys; the next test keeps it ignored
        if path.suffix in TEXT_SUFFIXES or path.name in {"Dockerfile", ".gitignore"}:
            files.append(path)
    return files


def test_no_api_keys_in_the_repository() -> None:
    offenders = []
    for path in tracked_text_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in KEY_PATTERNS:
            if pattern.search(text):
                offenders.append(str(path.relative_to(REPO_ROOT)))
                break
    assert not offenders, f"possible API keys committed in: {offenders}"


def test_dotenv_is_git_ignored_and_example_is_keyless() -> None:
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored and ".env.*" in ignored
    assert "!.env.example" in ignored, "the keyless template must stay committable"
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            if name.strip().endswith("_API_KEY"):
                assert value.strip() == "", f"{name.strip()} in .env.example must stay empty"
