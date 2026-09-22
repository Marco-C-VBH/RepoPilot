#!/usr/bin/env python
"""Build a RepoPilot-Bench task JSON from a task source directory.

    uv run python scripts/make_task.py evals/benchmark/sources/cachetools_001
    uv run python scripts/make_task.py SRC_DIR --dry-run          # derive, print, write nothing
    uv run python scripts/make_task.py SRC_DIR --refresh          # re-derive the audit fields only
    uv run python scripts/make_task.py --init SRC_DIR --id click_001 \\
        --repo https://github.com/pallets/click --commit <sha> \\
        --test-command "pytest tests/test_options.py"
    uv run python scripts/make_task.py --init SRC_DIR --id click_009 --source real \\
        --repo https://github.com/pallets/click --commit <parent-sha> --fix-commit <fix-sha>

The source directory holds task.toml, description.md, hidden.patch and (for a
mutation task) bug.patch -- see docs/benchmark-authoring.md.  gold_patch,
gold_files, gold_symbols, fail_to_pass and pass_to_pass are derived by building
the sandbox image and running the tests with and without the fix (needs
Docker); surface_symbols, surface_files, cross_module and difficulty by the
report audit against the buggy tree (git only).  ``--refresh`` re-runs just the
second half on an already-built task, so the audit fields of a shipped task
can be regenerated without Docker.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals.benchmark.authoring import (
    AuthoringError,
    init_draft,
    make_task,
    refresh_task,
    report,
)
from evals.benchmark.registry import TASKS_DIR
from repopilot.sandbox.docker import DockerError, docker_status
from repopilot.sandbox.repo import DEFAULT_CACHE_DIR, RepoError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source_dir", metavar="SRC_DIR", type=Path, help="task source directory")
    parser.add_argument("--out", type=Path, default=TASKS_DIR, help="where <id>.json goes")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--rebuild", action="store_true", help="rebuild the task image")
    parser.add_argument("--force", action="store_true", help="overwrite an existing task file")
    parser.add_argument("--dry-run", action="store_true", help="derive and report, write nothing")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-derive the audit fields (surface, cross_module, difficulty, suite, "
        "report_level) of an already-built task; no Docker, no test run",
    )

    init = parser.add_argument_group("--init: write template task.toml + description.md")
    init.add_argument("--init", action="store_true")
    init.add_argument("--id")
    init.add_argument("--repo")
    init.add_argument("--commit", help="the pinned commit (a real task: the fix's parent)")
    init.add_argument("--category", default="other")
    init.add_argument("--test-command", default="pytest tests")
    init.add_argument("--install", default="pip install -e .")
    init.add_argument("--suite", default="v1", choices=("v0", "v1"))
    init.add_argument(
        "--report-level", default="public_api", choices=("internal", "public_api", "symptom_only")
    )
    init.add_argument("--source", default="mutation", choices=("mutation", "real"))
    init.add_argument("--fix-commit", help="real tasks: the upstream fix commit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.init:
        if not (args.id and args.repo and args.commit):
            print("error: --init needs --id, --repo and --commit", file=sys.stderr)
            return 2
        if args.source == "real" and not args.fix_commit:
            print("error: --source real needs --fix-commit", file=sys.stderr)
            return 2
        written = init_draft(
            args.source_dir,
            task_id=args.id,
            repo=args.repo,
            commit=args.commit,
            category=args.category,
            test_command=args.test_command,
            install=args.install,
            suite=args.suite,
            report_level=args.report_level,
            fix_commit=args.fix_commit if args.source == "real" else None,
        )
        for path in written:
            print(f"wrote {path}")
        if not written:
            print(f"{args.source_dir}: task.toml and description.md already exist")
        if args.source == "real":
            print("next: create hidden.patch (the fix's tests, transplanted into a new file)")
        else:
            print(
                "next: create bug.patch and hidden.patch with `git diff` "
                "(docs/benchmark-authoring.md)"
            )
        return 0

    if args.refresh:
        try:
            result = refresh_task(
                args.source_dir, out_dir=args.out, cache_dir=args.cache_dir, dry_run=args.dry_run
            )
        except (AuthoringError, RepoError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(report(result))
        return 0

    available, detail = docker_status()
    if not available:
        print(f"error: {detail}", file=sys.stderr)
        return 2
    try:
        result = make_task(
            args.source_dir,
            out_dir=args.out,
            cache_dir=args.cache_dir,
            rebuild=args.rebuild,
            force=args.force,
            dry_run=args.dry_run,
        )
    except (AuthoringError, RepoError, DockerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
