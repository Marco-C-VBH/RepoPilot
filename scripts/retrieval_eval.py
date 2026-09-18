#!/usr/bin/env python
"""Offline retrieval ablation on the benchmark (spec §12.1): Recall@k and MRR per
configuration, with the bug report as the query.  No model calls, no Docker.

Usage:  uv run python scripts/retrieval_eval.py [--embedder local|hash] [--ids ...] [--k 10]

Writes results/retrieval-<timestamp>/{summary.json,results.jsonl}; archive the
run with scripts/archive_run.py like any other.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from uuid import uuid4

from evals.benchmark.registry import TASKS_DIR, TaskLoadError, load_tasks
from evals.retrieval import CONFIGS, evaluate_tasks, format_summary
from repopilot.retrieval.index import EMBEDDERS, make_embedder
from repopilot.sandbox.repo import DEFAULT_CACHE_DIR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tasks", type=Path, default=TASKS_DIR, help="task directory")
    parser.add_argument("--ids", nargs="*", metavar="ID", help="run only these task ids")
    parser.add_argument("--embedder", choices=EMBEDDERS, default="local")
    parser.add_argument("--k", type=int, default=10, help="results per query (default 10)")
    parser.add_argument("--out", type=Path, default=Path("results"), help="results root")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument(
        "--configs",
        default=",".join(CONFIGS),
        help=f"comma-separated subset of {','.join(CONFIGS)}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        tasks = load_tasks(args.tasks, args.ids)
    except TaskLoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    wanted = [c.strip() for c in args.configs.split(",") if c.strip()]
    unknown = [c for c in wanted if c not in CONFIGS]
    if unknown:
        print(f"error: unknown configuration(s) {unknown}; choose from {list(CONFIGS)}")
        return 2
    configs = {name: CONFIGS[name] for name in wanted}
    run_id = f"retrieval-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:4]}"
    out_dir = args.out / run_id
    print(f"run {run_id}: {len(tasks)} task(s), embedder={args.embedder}, k={args.k}")
    summary = evaluate_tasks(
        tasks,
        make_embedder(args.embedder),
        configs=configs,
        k=args.k,
        cache_dir=args.cache_dir,
        out_dir=out_dir,
    )
    print()
    print(format_summary(summary))
    print(f"results: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
