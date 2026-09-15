#!/usr/bin/env python
"""Scan agent traces for anything the agent must never see (docs/leak-audit.md).

Usage:  uv run python scripts/leak_scan.py [results/<run-id> ...]
        (default: every run under results/ and evals/experiments/)

For every ``tool_call`` event the arguments and output are searched for the
task's hidden test file paths and hidden test function names; every ``read_file``
path is checked to lie inside the repository.  Exit status 1 when anything is
found, so the scan can gate a benchmark change in CI once traces are archived.
It also prints how each model localizes (first tool call, whether the first
edit hit a gold file), the evidence behind the "localization is free" finding.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = REPO_ROOT / "evals" / "benchmark" / "tasks"
DEFAULT_ROOTS = (REPO_ROOT / "results", REPO_ROOT / "evals" / "experiments")

_NEW_FILE_RE = re.compile(r"^\+\+\+ b/(\S+)", re.MULTILINE)
_TEST_DEF_RE = re.compile(r"^\+\s*(?:async\s+)?def (test_\w+)", re.MULTILINE)


def load_tasks(tasks_dir: Path = TASKS_DIR) -> dict[str, dict]:
    tasks = {}
    for path in sorted(tasks_dir.glob("*.json")):
        task = json.loads(path.read_text(encoding="utf-8"))
        tasks[task["id"]] = task
    return tasks


def hidden_markers(task: dict) -> tuple[set[str], set[str]]:
    patch = task.get("hidden_test_patch") or ""
    return set(_NEW_FILE_RE.findall(patch)), set(_TEST_DEF_RE.findall(patch))


def run_dirs(roots: list[Path]) -> list[Path]:
    """Run directories under ``roots``; a run archived under evals/experiments is counted once."""
    runs: list[Path] = []
    for root in roots:
        if (root / "traces").is_dir():  # a single run directory
            runs.append(root)
        elif root.is_dir():
            runs.extend(sorted(p for p in root.iterdir() if (p / "traces").is_dir()))
    archived = set()
    for run in runs:
        summary = run / "summary.json"
        if summary.is_file():
            source = json.loads(summary.read_text(encoding="utf-8")).get("archived_from")
            if source:
                archived.add(Path(source).name)
    return [run for run in runs if run.name not in archived]


def trace_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for run in run_dirs(roots):
        files.extend(sorted((run / "traces").glob("*.jsonl")))
    return files


def scan(roots: list[Path], tasks: dict[str, dict]) -> tuple[list[str], dict[str, Counter]]:
    findings: list[str] = []
    by_model: dict[str, Counter] = defaultdict(Counter)
    for path in trace_files(roots):
        task_id = path.name.split(".")[0]
        task = tasks.get(task_id)
        if task is None:
            continue
        hidden_files, hidden_names = hidden_markers(task)
        events = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        ]
        model = next((e.get("model") for e in events if e.get("kind") == "run_start"), None) or "?"
        calls = [e for e in events if e.get("kind") == "tool_call"]
        for event in calls:
            arguments = event.get("arguments") or {}
            blob = json.dumps(arguments) + (event.get("output") or "")
            for marker in sorted(hidden_files | hidden_names):
                if marker in blob:
                    findings.append(f"{path}: {event['name']} exposed hidden marker {marker!r}")
            if event["name"] == "read_file":
                read_path = str(arguments.get("path", ""))
                if read_path.startswith(("/", "~")) or ".." in read_path.split("/"):
                    findings.append(f"{path}: read_file outside the repository: {read_path!r}")
        if not calls:
            continue
        stats = by_model[model]
        stats["runs"] += 1
        stats[f"first call: {calls[0]['name']}"] += 1
        edits = [c for c in calls if c["name"] == "edit_file"]
        if edits:
            stats["runs with an edit"] += 1
            if (edits[0].get("arguments") or {}).get("path") in task["gold_files"]:
                stats["first edit in a gold file"] += 1
        reads = [c for c in calls if c["name"] == "read_file"]
        if reads and (reads[0].get("arguments") or {}).get("path") in task["gold_files"]:
            stats["first read is a gold file"] += 1
        before_edit = calls[: calls.index(edits[0])] if edits else calls
        if any(c["name"] == "run_tests" for c in before_edit):
            stats["ran tests before the first edit"] += 1
    return findings, by_model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("roots", nargs="*", type=Path, default=list(DEFAULT_ROOTS))
    args = parser.parse_args(argv)
    tasks = load_tasks()
    findings, by_model = scan(args.roots, tasks)
    for model, stats in by_model.items():
        print(model)
        for key, value in sorted(stats.items()):
            print(f"  {key}: {value}")
    total = sum(s["runs"] for s in by_model.values())
    if findings:
        print(f"\n{len(findings)} finding(s) in {total} trace(s):")
        for line in findings:
            print("  " + line)
        return 1
    print(f"\n{total} trace(s) scanned: no hidden test file or test name reached the agent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
