#!/usr/bin/env python
"""Regenerate evals/benchmark/task.schema.json from the pydantic Task model.

Run this after changing evals/benchmark/schema.py; tests fail if the exported
file is stale.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.benchmark.schema import task_json_schema

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "evals" / "benchmark" / "task.schema.json"


def render() -> str:
    return json.dumps(task_json_schema(), indent=2) + "\n"


def main() -> int:
    SCHEMA_PATH.write_text(render(), encoding="utf-8")
    print(f"wrote {SCHEMA_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
