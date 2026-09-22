#!/usr/bin/env python
"""Ask models to write the benchmark's gold symbols from memory (bench-v1-design.md §9.3).

    uv run python scripts/memorization_probe.py --models claude-haiku-4-5-20251001 gpt-5.6-luna
    uv run python scripts/memorization_probe.py --suite v1-new --max-cost 1

One call per gold symbol per model, no tools: the package, the pinned commit,
the file and the def line, then "write the rest".  The reply's similarity to
the pinned source (normalized tokens) says how much of the fix a model could
produce by recall alone; ``summary.json`` has the recalled share per suite and
repository, and the rows can be joined with a leak-ablation run by task id.
Needs API keys (.env) and git; no Docker.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from uuid import uuid4

from evals.benchmark.registry import TASKS_DIR, TaskLoadError, load_tasks
from evals.memorization import format_summary, run_probe
from evals.runner import SUITES, select_suite
from repopilot.models.client import client_for
from repopilot.models.config import ConfigError, api_key_for, load_env, provider_for
from repopilot.sandbox.repo import DEFAULT_CACHE_DIR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tasks", type=Path, default=TASKS_DIR)
    parser.add_argument("--ids", nargs="*", metavar="ID")
    parser.add_argument("--suite", choices=SUITES)
    parser.add_argument("--models", nargs="+", required=True, help="model ids to probe")
    parser.add_argument("--out", type=Path, default=Path("results"))
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--max-cost", type=float, default=2.0, help="USD cap for the whole probe")
    parser.add_argument("--env-file", default=".env")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        tasks = select_suite(load_tasks(args.tasks, args.ids), args.suite)
    except TaskLoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    load_env(args.env_file)
    try:
        for model in args.models:
            api_key_for(provider_for(model))
        clients = [client_for(model) for model in args.models]
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    run_id = f"memorization-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:4]}"
    out_dir = args.out / run_id
    symbols = sum(len(t.gold_symbols) for t in tasks)
    print(
        f"run {run_id}: {len(tasks)} task(s), {symbols} symbol(s) × {len(clients)} model(s); "
        f"cap ${args.max_cost:.2f}"
    )
    summary = run_probe(
        tasks, clients, out_dir=out_dir, cache_dir=args.cache_dir, max_cost_usd=args.max_cost
    )
    print()
    print(format_summary(summary))
    print(f"results: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
