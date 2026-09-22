#!/usr/bin/env python
"""Validate every task file, print one line per task and the shape of the suite.

Usage:  uv run python scripts/validate_tasks.py [TASKS_DIR] [--audit] [--strict]

Every file must load against the schema; the per-task line shows the v1
fields (suite, report tier, hidden-only, cross-module, fix shape, derived
difficulty).  The suite report at the end counts the whole benchmark and
checks the 36 v1 tasks against the structural targets of
docs/bench-v1-design.md §2.  ``--audit`` re-runs the report audit of every
task against its tree (git only, no Docker; a few seconds per task) and fails
on a violation; ``--strict`` also exits 1 when a v1 target is missed or a task
has not been audited.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals.benchmark.authoring import (
    SOURCES_DIR,
    AuthoringError,
    audit_draft,
    load_draft,
    normalize,
)
from evals.benchmark.registry import TASKS_DIR, TaskLoadError, load_tasks
from evals.benchmark.suite import format_suite_report, suite_report
from repopilot.sandbox.repo import DEFAULT_CACHE_DIR, RepoError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tasks_dir", nargs="?", type=Path, default=TASKS_DIR)
    parser.add_argument("--sources", type=Path, default=SOURCES_DIR, help="task source dirs")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--audit", action="store_true", help="re-run the report audit per task")
    parser.add_argument(
        "--strict", action="store_true", help="exit 1 on a missed v1 target or an unaudited task"
    )
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
        f"{'id':<18} {'suite':<5} {'source':<8} {'category':<19} {'tier':<12} "
        f"{'hidden':<6} {'cross':<5} {'shape':<11} {'diff':<6} {'f2p':>4} {'p2p':>4}  gold_files"
    )
    print(header)
    print("-" * len(header))
    for t in tasks:
        print(
            f"{t.id:<18} {t.suite:<5} {t.source:<8} {t.category:<19} {t.report_level:<12} "
            f"{'yes' if t.hidden_only else 'no':<6} {'yes' if t.cross_module else 'no':<5} "
            f"{t.shape.label:<11} {t.difficulty:<6} {len(t.fail_to_pass):>4} "
            f"{len(t.pass_to_pass):>4}  {', '.join(t.gold_files)}"
        )
    print(f"\n{len(tasks)} task(s) valid\n")

    report = suite_report(tasks)
    print(format_suite_report(report))

    failed = False
    if args.audit:
        print("\nreport audit:")
        for t in tasks:
            source = args.sources / t.id
            try:
                draft = load_draft(source)
                patches = normalize(draft, cache_dir=args.cache_dir)
                audit = audit_draft(draft, patches)
            except (AuthoringError, RepoError) as exc:
                print(f"  {t.id:<18} ERROR {exc}")
                failed = True
                continue
            status = "ok" if audit.report.ok else "VIOLATION"
            failed |= not audit.report.ok
            extra = ""
            if audit.report.cross_module != t.cross_module:
                extra = "  (cross_module differs from the task file: run make_task --refresh)"
                failed = True
            print(f"  {t.id:<18} {status} {'; '.join(audit.report.violations)}{extra}")

    if args.strict:
        missed = [k for k, v in report["targets"].items() if not v["met"]]
        if report["v1_new"]["tasks"] and missed:
            print(f"\nstrict: v1 targets missed: {', '.join(missed)}")
            failed = True
        if report["unaudited"] or report["difficulty_mismatch"]:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
