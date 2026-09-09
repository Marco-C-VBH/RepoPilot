"""Unit tests for the task registry (evals/benchmark/registry.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evals.benchmark.registry import TaskLoadError, load_task, load_tasks
from tests.test_schema import make_task


def write_task(tasks_dir: Path, filename: str, data: dict[str, Any] | str) -> Path:
    path = tasks_dir / filename
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_loads_sorted_by_filename(tmp_path: Path) -> None:
    write_task(tmp_path, "b_002.json", make_task(id="b_002"))
    write_task(tmp_path, "a_001.json", make_task(id="a_001"))
    assert [t.id for t in load_tasks(tmp_path)] == ["a_001", "b_002"]


def test_ids_filter_preserves_requested_order(tmp_path: Path) -> None:
    write_task(tmp_path, "a_001.json", make_task(id="a_001"))
    write_task(tmp_path, "b_002.json", make_task(id="b_002"))
    assert [t.id for t in load_tasks(tmp_path, ids=["b_002", "a_001"])] == ["b_002", "a_001"]


def test_unknown_ids_raise(tmp_path: Path) -> None:
    write_task(tmp_path, "a_001.json", make_task(id="a_001"))
    with pytest.raises(TaskLoadError, match=r"unknown task ids: \['nope'\]"):
        load_tasks(tmp_path, ids=["nope"])


def test_filename_must_match_id(tmp_path: Path) -> None:
    write_task(tmp_path, "wrong_name.json", make_task(id="a_001"))
    with pytest.raises(TaskLoadError, match="filename must be 'a_001.json'"):
        load_tasks(tmp_path)


def test_draft_files_are_skipped(tmp_path: Path) -> None:
    write_task(tmp_path, "a_001.json", make_task(id="a_001"))
    write_task(tmp_path, "_draft.json", "{ this is not even json")
    assert [t.id for t in load_tasks(tmp_path)] == ["a_001"]


def test_invalid_json_reports_path(tmp_path: Path) -> None:
    path = write_task(tmp_path, "a_001.json", "{ nope")
    with pytest.raises(TaskLoadError, match=str(path)):
        load_task(path)


def test_schema_violation_reports_path_and_field(tmp_path: Path) -> None:
    path = write_task(tmp_path, "a_001.json", make_task(id="a_001", base_commit="abc123"))
    with pytest.raises(TaskLoadError) as excinfo:
        load_tasks(tmp_path)
    assert str(path) in str(excinfo.value)
    assert "base_commit" in str(excinfo.value)


def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(TaskLoadError, match="tasks directory not found"):
        load_tasks(tmp_path / "nowhere")


def test_empty_directory_is_empty_benchmark(tmp_path: Path) -> None:
    assert load_tasks(tmp_path) == []
