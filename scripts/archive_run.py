#!/usr/bin/env python
"""Keep a benchmark run as evidence: copy its summary, results and traces into the repo.

Usage:  uv run python scripts/archive_run.py results/<run-id> <name>
        uv run python scripts/archive_run.py results/baseline-2026... baseline-v0-sonnet5

Runs land in ``results/`` (git-ignored, everything including full test output);
the numbers quoted in the README must be reproducible from the repository, so
the run behind each quoted number is archived under ``evals/experiments/<name>/``:
``summary.json``, ``results.jsonl`` and ``traces/`` (the per-task logs stay out --
they hold the full pytest output and are only useful while debugging).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

EXPERIMENTS_DIR = Path(__file__).resolve().parent.parent / "evals" / "experiments"
KEEP = ("summary.json", "results.jsonl")


def archive(
    run_dir: Path, name: str, *, out_root: Path = EXPERIMENTS_DIR, force: bool = False
) -> Path:
    if not (run_dir / "summary.json").is_file():
        raise SystemExit(f"error: {run_dir} has no summary.json (not a runner output directory)")
    target = out_root / name
    if target.exists():
        if not force:
            raise SystemExit(f"error: {target} exists; pass --force to replace it")
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for filename in KEEP:
        shutil.copyfile(run_dir / filename, target / filename)
    traces = run_dir / "traces"
    if traces.is_dir():
        shutil.copytree(traces, target / "traces")
    summary = json.loads((target / "summary.json").read_text(encoding="utf-8"))
    summary["archived_from"] = str(run_dir)
    (target / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("name", help="directory name under evals/experiments/")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    target = archive(args.run_dir, args.name, force=args.force)
    summary = json.loads((target / "summary.json").read_text(encoding="utf-8"))
    agent = summary.get("agent") or {}
    print(f"archived {args.run_dir} -> {target}")
    if summary.get("kind") == "retrieval":  # scripts/retrieval_eval.py output
        print(
            f"  retrieval eval · embedder {summary.get('embedder')} · {summary.get('tasks')} "
            f"task(s) · {', '.join(summary.get('configs', {}))}"
        )
        return 0
    print(
        f"  {summary['solver']} · {summary.get('model') or '-'} · {summary['tasks']} task(s) × "
        f"{summary['repeat']} · pass rate {summary['pass_rate']:.1%}"
        + (f" · cost ${agent.get('cost_total_usd', 0):.2f}" if agent else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
