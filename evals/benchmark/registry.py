"""Loading and validating RepoPilot-Bench task files.

Layout: one JSON file per task in ``evals/benchmark/tasks/<id>.json``.  Files
whose name starts with ``_`` are drafts and are skipped.  The filename must
equal the task id so a task can be found without opening every file.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError

from evals.benchmark.schema import Task

TASKS_DIR = Path(__file__).resolve().parent / "tasks"


class TaskLoadError(ValueError):
    """A task file is missing, malformed JSON, or violates the task schema."""


def load_task(path: Path) -> Task:
    """Parse and validate a single task file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskLoadError(f"{path}: {exc}") from exc
    try:
        return Task.model_validate(data)
    except ValidationError as exc:
        raise TaskLoadError(f"{path}:\n{exc}") from exc


def load_tasks(tasks_dir: Path = TASKS_DIR, ids: Iterable[str] | None = None) -> list[Task]:
    """Load every ``*.json`` task in ``tasks_dir``, sorted by filename.

    ``ids`` restricts the result to those tasks, in the order given; an unknown
    id is an error rather than a silent omission, so a benchmark run can never
    quietly shrink.
    """
    if not tasks_dir.is_dir():
        raise TaskLoadError(f"tasks directory not found: {tasks_dir}")

    tasks: dict[str, Task] = {}
    for path in sorted(tasks_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        task = load_task(path)
        if path.stem != task.id:
            raise TaskLoadError(f"{path}: filename must be '{task.id}.json' (task id)")
        if task.id in tasks:
            raise TaskLoadError(f"{path}: duplicate task id {task.id!r}")
        tasks[task.id] = task

    if ids is None:
        return list(tasks.values())

    wanted = list(ids)
    unknown = [i for i in wanted if i not in tasks]
    if unknown:
        raise TaskLoadError(f"unknown task ids: {unknown} (available: {sorted(tasks)})")
    return [tasks[i] for i in wanted]
