"""RepoPilot-Bench runner (spec §10.1, §15.1).

    python -m evals.runner --solver gold                     # every task should PASS
    python -m evals.runner --solver null --expect fail       # every task should FAIL
    python -m evals.runner --solver gold --repeat 2          # determinism check
    python -m evals.runner --solver gold --ids cachetools_001 cachetools_002
    python -m evals.runner --solver baseline --model claude-sonnet-5 --max-run-cost 10
    python -m evals.runner --solver structured --model claude-haiku-4-5-20251001 --max-run-cost 10
    python -m evals.runner --list

Each run writes ``results/<solver>-<timestamp>-<id>/`` containing
``results.jsonl`` (one ``TaskResult`` per task and repeat), ``summary.json``
and ``logs/<task_id>[.<repeat>].log`` with the patch, the test output and the
per-test verdict table.  Agent solvers also write ``traces/<task_id>.jsonl``
(spec §13.1) and get an ``agent`` block in the summary (spec §11.1 metrics and
the §11.2 failure taxonomy).

Phase 0 exit criterion: ``--solver null --expect fail`` and
``--solver gold --expect pass --repeat 2`` both exit 0 -- every task fails
untouched, every task passes with its reference fix, and repeated runs agree.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from evals.benchmark.registry import TASKS_DIR, TaskLoadError, load_tasks
from evals.benchmark.schema import Task
from evals.harness import TaskResult, run_task
from evals.judge import Status
from evals.metrics import agent_metrics, classify_failure, format_agent_metrics
from evals.solvers import AGENT_SOLVERS, SOLVERS, AgentSolver, Solver, get_solver
from repopilot.agent.budget import DEFAULT_BUDGET, AgentBudget
from repopilot.models.config import (
    ConfigError,
    ModelSettings,
    api_key_for,
    load_env,
    provider_for,
)
from repopilot.sandbox.docker import docker_status
from repopilot.sandbox.limits import DEFAULT_LIMITS
from repopilot.sandbox.repo import DEFAULT_CACHE_DIR


class RunSummary(BaseModel):
    run_id: str
    solver: str
    tasks: int
    repeat: int
    counts: dict[str, int]
    pass_rate: float
    deterministic: bool | None = None
    nondeterministic_tasks: list[str] = Field(default_factory=list)
    duration_seconds: float
    started_at: str
    finished_at: str
    limits: dict[str, object]
    results_dir: str
    model: str | None = None
    budget: dict[str, object] | None = None
    agent: dict[str, object] | None = None
    stopped_early: str | None = None  # why the run ended before every task ran


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.runner",
        description="Run a solver over RepoPilot-Bench tasks and judge every result.",
    )
    parser.add_argument(
        "--tasks", type=Path, default=TASKS_DIR, help="directory of <id>.json task files"
    )
    parser.add_argument("--ids", nargs="*", metavar="ID", help="run only these task ids")
    parser.add_argument("--solver", choices=sorted(SOLVERS), default="null")
    parser.add_argument("--out", type=Path, default=Path("results"), help="results root")
    parser.add_argument("--repeat", type=int, default=1, help="run every task N times")
    parser.add_argument("--rebuild", action="store_true", help="rebuild task images")
    parser.add_argument(
        "--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="repo mirror cache"
    )
    parser.add_argument(
        "--expect",
        choices=["pass", "fail"],
        help="exit 1 unless every result has this status (oracle validation)",
    )
    parser.add_argument("--list", action="store_true", help="list the selected tasks and exit")
    agent = parser.add_argument_group("agent solvers")
    agent.add_argument(
        "--model", help="model id for the agent (default: REPOPILOT_STRONG_MODEL or the built-in)"
    )
    agent.add_argument(
        "--max-steps", type=int, help=f"model calls per task (default {DEFAULT_BUDGET.max_steps})"
    )
    agent.add_argument(
        "--max-tool-calls", type=int, help=f"default {DEFAULT_BUDGET.max_tool_calls}"
    )
    agent.add_argument("--max-test-runs", type=int, help=f"default {DEFAULT_BUDGET.max_test_runs}")
    agent.add_argument(
        "--max-tokens", type=int, help=f"per task (default {DEFAULT_BUDGET.max_tokens})"
    )
    agent.add_argument(
        "--max-cost", type=float, help=f"USD per task (default {DEFAULT_BUDGET.max_cost_usd})"
    )
    agent.add_argument(
        "--max-runtime",
        type=float,
        help=f"seconds per task (default {DEFAULT_BUDGET.max_runtime_seconds})",
    )
    agent.add_argument(
        "--max-run-cost",
        type=float,
        help="USD cap for the whole run: stop starting tasks once the estimated spend passes it",
    )
    agent.add_argument("--env-file", default=".env", help="where API keys are loaded from")
    return parser


def budget_from_args(args: argparse.Namespace) -> AgentBudget:
    return DEFAULT_BUDGET.replace(
        max_steps=args.max_steps,
        max_tool_calls=args.max_tool_calls,
        max_test_runs=args.max_test_runs,
        max_tokens=args.max_tokens,
        max_cost_usd=args.max_cost,
        max_runtime_seconds=args.max_runtime,
    )


def list_tasks(tasks: list[Task], tasks_dir: Path) -> None:
    for task in tasks:
        print(f"{task.id:<24} {task.source:<9} {task.category:<20} {task.difficulty}")
    print(f"{len(tasks)} task(s) selected from {tasks_dir}")


def run_benchmark(
    tasks: list[Task],
    solver: Solver | str,
    *,
    out_root: Path,
    repeat: int = 1,
    rebuild: bool = False,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    max_run_cost: float | None = None,
) -> tuple[RunSummary, list[TaskResult]]:
    if isinstance(solver, str):
        solver = get_solver(solver)
    solver_name = solver.name
    run_id = f"{solver_name}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:4]}"
    out_dir = out_root / run_id
    logs_dir = out_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    traces_dir = out_dir / "traces"
    results_path = out_dir / "results.jsonl"
    by_id = {t.id: t for t in tasks}

    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    t0 = time.monotonic()
    results: list[TaskResult] = []
    spent = 0.0
    stopped_early: str | None = None
    print(f"run {run_id}: {len(tasks)} task(s) × {repeat}, solver={solver_name}")
    if isinstance(solver, AgentSolver):
        b = solver.budget
        print(
            f"model={solver.model} budget: {b.max_steps} steps, {b.max_tool_calls} tool calls, "
            f"{b.max_test_runs} test runs, {b.max_tokens} tokens, ${b.max_cost_usd:.2f}, "
            f"{b.max_runtime_seconds:.0f}s per task"
            + (f"; run cap ${max_run_cost:.2f}" if max_run_cost is not None else "")
        )
    print(f"{'task':<24} {'status':<6} {'reasons':<28} {'tests':<34} {'time':>7}")

    with results_path.open("a", encoding="utf-8") as sink:
        for index in range(repeat):
            for task in tasks:
                if max_run_cost is not None and spent > max_run_cost:
                    stopped_early = f"run cost ${spent:.2f} passed the ${max_run_cost:.2f} cap"
                    break
                result, log = run_task(
                    task,
                    solver,
                    cache_dir=cache_dir,
                    rebuild=rebuild and index == 0,
                    repeat=index,
                    trace_dir=traces_dir,
                )
                failure = classify_failure(by_id[task.id], result)
                result.failure = str(failure) if failure else None
                results.append(result)
                sink.write(result.model_dump_json() + "\n")
                sink.flush()
                suffix = f".{index}" if repeat > 1 else ""
                (logs_dir / f"{task.id}{suffix}.log").write_text(log, encoding="utf-8")
                print(_format_row(result))
                if result.agent:
                    spent += float(result.agent.get("cost_usd", 0.0))
            if stopped_early:
                break

    summary = _summarize(results, run_id, solver_name, len(tasks), repeat, started_at, t0, out_dir)
    if isinstance(solver, AgentSolver):
        summary.model = solver.model
        summary.budget = solver.budget.to_record()
    summary.agent = agent_metrics(tasks, results)
    summary.stopped_early = stopped_early
    (out_dir / "summary.json").write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    return summary, results


def _format_row(result: TaskResult) -> str:
    counts = result.test_counts
    tests = (
        f"{counts.get('passed', 0)}P {counts.get('failed', 0)}F "
        f"{counts.get('error', 0)}E {counts.get('skipped', 0)}S"
        if counts
        else "-"
    )
    if result.timed_out:
        tests += " timeout"
    total = result.durations.get("total", 0.0)
    label = f"{result.task_id}.{result.repeat}" if result.repeat else result.task_id
    row = f"{label:<24} {result.status:<6} {result.reason:<28} {tests:<34} {total:>6.1f}s"
    if result.agent:
        a = result.agent
        row += (
            f"  {a.get('termination', '?')} · {a.get('steps', 0)} steps · "
            f"${float(a.get('cost_usd', 0.0)):.3f}"
        )
        if result.failure:
            row += f" · {result.failure}"
    return row


def _summarize(
    results: list[TaskResult],
    run_id: str,
    solver: str,
    n_tasks: int,
    repeat: int,
    started_at: str,
    t0: float,
    out_dir: Path,
) -> RunSummary:
    counts = Counter(str(r.status) for r in results)
    for status in Status:
        counts.setdefault(str(status), 0)

    deterministic: bool | None = None
    flaky: list[str] = []
    if repeat > 1:
        by_task: dict[str, set[tuple[object, ...]]] = defaultdict(set)
        for r in results:
            by_task[r.task_id].add(r.signature())
        flaky = sorted(t for t, sigs in by_task.items() if len(sigs) > 1)
        deterministic = not flaky

    total = len(results)
    return RunSummary(
        run_id=run_id,
        solver=solver,
        tasks=n_tasks,
        repeat=repeat,
        counts=dict(sorted(counts.items())),
        pass_rate=round(counts["PASS"] / total, 4) if total else 0.0,
        deterministic=deterministic,
        nondeterministic_tasks=flaky,
        duration_seconds=round(time.monotonic() - t0, 3),
        started_at=started_at,
        finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
        limits=DEFAULT_LIMITS.__dict__.copy(),
        results_dir=str(out_dir),
    )


def print_summary(summary: RunSummary) -> None:
    c = summary.counts
    line = (
        f"\n{summary.solver}: {summary.tasks} task(s) × {summary.repeat} -> "
        f"{c['PASS']} PASS, {c['FAIL']} FAIL, {c['ERROR']} ERROR "
        f"(pass rate {summary.pass_rate:.1%}) in {summary.duration_seconds:.0f}s"
    )
    print(line)
    if summary.deterministic is not None:
        if summary.deterministic:
            print(f"deterministic: yes ({summary.tasks}/{summary.tasks} identical across runs)")
        else:
            print(
                f"deterministic: NO -- differing tasks: {', '.join(summary.nondeterministic_tasks)}"
            )
    if summary.agent:
        print(format_agent_metrics(summary.agent))
    if summary.stopped_early:
        print(f"stopped early: {summary.stopped_early}")
    print(f"results: {summary.results_dir}")


def build_solver(args: argparse.Namespace) -> Solver:
    """The solver the CLI asked for; agent solvers get model, budget and keys checked."""
    if args.solver not in AGENT_SOLVERS:
        return get_solver(args.solver)
    load_env(args.env_file)
    settings = ModelSettings.from_env(strong=args.model)
    api_key_for(provider_for(settings.strong))  # both agents use the strong model only
    return get_solver(
        args.solver, model=settings.strong, budget=budget_from_args(args), cache_dir=args.cache_dir
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repeat < 1:
        print("error: --repeat must be >= 1", file=sys.stderr)
        return 2
    try:
        tasks = load_tasks(args.tasks, args.ids)
    except TaskLoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.list:
        list_tasks(tasks, args.tasks)
        return 0
    if not tasks:
        print(f"error: no tasks in {args.tasks}", file=sys.stderr)
        return 2

    available, detail = docker_status()
    if not available:
        print(f"error: {detail}", file=sys.stderr)
        return 2

    try:
        solver = build_solver(args)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    summary, results = run_benchmark(
        tasks,
        solver,
        out_root=args.out,
        repeat=args.repeat,
        rebuild=args.rebuild,
        cache_dir=args.cache_dir,
        max_run_cost=args.max_run_cost,
    )
    print_summary(summary)

    exit_code = 0
    if summary.counts["ERROR"]:
        print(f"{summary.counts['ERROR']} ERROR result(s): the harness, not the solver, failed")
        exit_code = 1
    if args.expect:
        expected = Status.PASS if args.expect == "pass" else Status.FAIL
        unexpected = [r for r in results if r.status is not expected]
        if unexpected:
            print(f"expectation '{args.expect}' violated by {len(unexpected)} result(s):")
            for r in unexpected:
                print(f"  {r.task_id}.{r.repeat}: {r.status} {r.reason}")
            exit_code = 1
        else:
            print(f"expectation '{args.expect}' met by all {len(results)} result(s)")
    if summary.deterministic is False:
        exit_code = 1
    return exit_code


def load_results(path: Path) -> list[TaskResult]:
    """Read a results.jsonl back (for analysis scripts and tests)."""
    with path.open(encoding="utf-8") as fh:
        return [TaskResult.model_validate(json.loads(line)) for line in fh if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
