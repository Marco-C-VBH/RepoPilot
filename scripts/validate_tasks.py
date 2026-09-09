#!/usr/bin/env python
"""Validate every task file and print a one-line summary per task.

Usage:  uv run python scripts/validate_tasks.py [TASKS_DIR]

Exit code 1 on the first invalid file, with the pydantic error report.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals.benchmark.registry import TASKS_DIR, TaskLoadError, load_tasks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tasks_dir", nargs="?", type=Path, default=TASKS_DIR)
    args = parser.parse_args(argv)

    try:
        tasks = load_tasks(args.tasks_dir)
    except TaskLoadError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not tasks:
        print(f"no task files in {args.tasks_dir}")
        return 0

    header = (
        f"{'id':<24} {'source':<9} {'category':<20} {'difficulty':<10} "
        f"{'f2p':>4} {'p2p':>4}  gold_files"
    )
    print(header)
    print("-" * len(header))
    for t in tasks:
        print(
            f"{t.id:<24} {t.source:<9} {t.category:<20} {t.difficulty:<10} "
            f"{len(t.fail_to_pass):>4} {len(t.pass_to_pass):>4}  {', '.join(t.gold_files)}"
        )
    print(f"\n{len(tasks)} task(s) valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
