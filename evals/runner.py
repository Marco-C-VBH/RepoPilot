"""RepoPilot-Bench runner -- Phase 0, step 3 (solver execution not implemented yet).

Target CLI (spec §10.1, §15.1):

    python -m evals.runner --list                 # tasks that would run
    python -m evals.runner --solver null          # apply nothing: every task must FAIL
    python -m evals.runner --solver gold          # apply gold_patch: every task must PASS
    python -m evals.runner --solver gold --ids cachetools_001 cachetools_002

Phase 0 exit criterion: ``null`` -> 0/N, ``gold`` -> N/N, and a second run of
each gives identical per-task results.  Only then does a real agent get plugged
in as another solver (Phase 1).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals.benchmark.registry import TASKS_DIR, TaskLoadError, load_tasks

SOLVERS = ("null", "gold")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evals.runner", description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--tasks", type=Path, default=TASKS_DIR, help="directory of <id>.json task files"
    )
    parser.add_argument("--ids", nargs="*", metavar="ID", help="run only these task ids")
    parser.add_argument("--solver", choices=SOLVERS, default="null")
    parser.add_argument("--out", type=Path, default=Path("results"), help="results directory")
    parser.add_argument("--list", action="store_true", help="list the selected tasks and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        tasks = load_tasks(args.tasks, args.ids)
    except TaskLoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.list:
        for task in tasks:
            print(f"{task.id:<24} {task.source:<9} {task.category:<20} {task.difficulty}")
        print(f"{len(tasks)} task(s) selected from {args.tasks}")
        return 0

    print(
        f"error: solver execution is not implemented yet (Phase 0 step 2/3: Docker sandbox + "
        f"runner). {len(tasks)} task(s) would run with solver={args.solver!r}.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
