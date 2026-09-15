"""Agent metrics (spec §11.1) and the failure taxonomy (spec §11.2).

Everything here is computed from ``TaskResult`` records -- the verdict plus the
``agent`` summary a solver attached -- so a results.jsonl can be re-aggregated
long after the run.  The failure classification is a *heuristic* first cut
that needs no human labels: it combines the judge's reason codes, how the run
ended, and whether the agent ever inspected or edited a gold file.  It is
meant to point at the highest-leverage problem (retrieval vs. reasoning vs.
patching vs. budget), not to be a ground truth.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from evals.benchmark.schema import Task
from evals.harness import TaskResult
from evals.judge import Reason, Status


class Failure(StrEnum):
    ENVIRONMENT_FAILURE = "environment_failure"  # harness / sandbox / provider outage
    BUDGET_EXCEEDED = "budget_exceeded"  # ran out of steps, tool calls, tokens, cost or time
    RETRIEVAL_FAILURE = "retrieval_failure"  # never read or edited a gold file
    REASONING_FAILURE = "reasoning_failure"  # read a gold file but made no edit at all
    WRONG_LOCALIZATION = "wrong_localization"  # edited files, none of them a gold file
    INCORRECT_PATCH = "incorrect_patch"  # edited a gold file; tests still fail
    REGRESSION_INTRODUCED = "regression_introduced"  # broke passing tests
    TEST_MISUNDERSTANDING = "test_misunderstanding"  # edited tests instead of the source
    TOOL_FAILURE = "tool_failure"  # the patch could not even be applied
    AGENT_LOOP = "agent_loop"  # the runtime's loop detector ended the run


def classify_failure(task: Task, result: TaskResult) -> Failure | None:
    """Why a non-passing result failed, or None for a PASS."""
    if result.status is Status.PASS:
        return None
    if result.status is Status.ERROR:
        return Failure.ENVIRONMENT_FAILURE
    reasons = set(result.reasons)
    agent = result.agent or {}
    termination = agent.get("termination", "")

    if termination == "model_error":
        return Failure.ENVIRONMENT_FAILURE
    if termination == "agent_loop":
        return Failure.AGENT_LOOP
    if Reason.PATCH_APPLY_FAILED in reasons:
        return Failure.TOOL_FAILURE
    if Reason.PASS_TO_PASS_REGRESSED in reasons:
        return Failure.REGRESSION_INTRODUCED
    if termination.startswith("budget_"):
        return Failure.BUDGET_EXCEEDED

    gold = set(task.gold_files)
    edited = set(agent.get("files_edited", [])) | set(agent.get("changed_files", []))
    read = set(agent.get("files_read", []))
    if result.patch_test_files and not edited - set(result.patch_test_files):
        return Failure.TEST_MISUNDERSTANDING
    if not edited:
        return Failure.REASONING_FAILURE if gold & read else Failure.RETRIEVAL_FAILURE
    if not gold & edited:
        return Failure.WRONG_LOCALIZATION if gold & read else Failure.RETRIEVAL_FAILURE
    return Failure.INCORRECT_PATCH


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def _mean(values: Sequence[float]) -> float:
    return round(statistics.fmean(values), 3) if values else 0.0


def _median(values: Sequence[float]) -> float:
    return round(statistics.median(values), 4) if values else 0.0


def agent_metrics(tasks: Sequence[Task], results: Sequence[TaskResult]) -> dict[str, Any] | None:
    """Aggregate the agent summaries of a run; None when no result carries one."""
    with_agent = [r for r in results if r.agent]
    if not with_agent:
        return None
    by_id = {t.id: t for t in tasks}
    records = [r.agent or {} for r in with_agent]

    passes = sum(1 for r in with_agent if r.status is Status.PASS)
    patched = sum(1 for r in with_agent if r.patch_bytes > 0)
    regressions = sum(1 for r in with_agent if Reason.PASS_TO_PASS_REGRESSED in r.reasons)
    tool_calls = sum(int(a.get("tool_calls", 0)) for a in records)
    invalid = sum(int(a.get("invalid_tool_calls", 0)) for a in records)
    costs = [float(a.get("cost_usd", 0.0)) for a in records]
    tokens = [int(a.get("input_tokens", 0)) + int(a.get("output_tokens", 0)) for a in records]
    solve_times = [float(r.durations.get("solve", 0.0)) for r in with_agent]

    failures = Counter()
    by_category: dict[str, Counter] = {}
    by_difficulty: dict[str, Counter] = {}
    for r in with_agent:
        task = by_id.get(r.task_id)
        if task is None:
            continue
        failure = r.failure or (str(classify_failure(task, r) or "") or None)
        if failure:
            failures[failure] += 1
        for bucket, key in (
            (by_category, str(task.category)),
            (by_difficulty, str(task.difficulty)),
        ):
            counter = bucket.setdefault(key, Counter())
            counter["total"] += 1
            counter["pass"] += r.status is Status.PASS

    n = len(with_agent)
    metrics = {
        "results": n,
        "success_rate": round(passes / n, 4),
        "patch_rate": round(patched / n, 4),
        "regression_rate": round(regressions / n, 4),
        "avg_steps": _mean([int(a.get("steps", 0)) for a in records]),
        "avg_tool_calls": _mean([int(a.get("tool_calls", 0)) for a in records]),
        "avg_test_runs": _mean([int(a.get("test_runs", 0)) for a in records]),
        "invalid_tool_call_rate": round(invalid / tool_calls, 4) if tool_calls else 0.0,
        "terminations": dict(Counter(str(a.get("termination", "?")) for a in records)),
        "tokens_mean": round(_mean(tokens)),
        "tokens_median": round(_median(tokens)),
        "cost_total_usd": round(sum(costs), 4),
        "cost_mean_usd": round(_mean(costs), 4),
        "cost_median_usd": _median(costs),
        "solve_p50_seconds": round(_percentile(solve_times, 0.5), 1),
        "solve_p95_seconds": round(_percentile(solve_times, 0.95), 1),
        "failures": dict(sorted(failures.items())),
        "by_category": {
            k: {"pass": int(v["pass"]), "total": int(v["total"])}
            for k, v in sorted(by_category.items())
        },
        "by_difficulty": {
            k: {"pass": int(v["pass"]), "total": int(v["total"])}
            for k, v in sorted(by_difficulty.items())
        },
    }
    runtime = runtime_metrics(records)
    if runtime:
        metrics["runtime"] = runtime
    return metrics


RUNTIME_COUNTERS = (
    "loop_interventions",
    "repeated_tool_calls",
    "refused_tool_calls",
    "forced_transitions",
    "nudges",
    "analyze_rounds",
    "workspace_resets",
    "finalize_continues",
)


def runtime_metrics(records: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Aggregates of the structured runtime's counters; None for baseline runs."""
    runs = [a["runtime"] for a in records if isinstance(a.get("runtime"), dict)]
    if not runs:
        return None
    n = len(runs)
    steps_by_phase: Counter = Counter()
    for r in runs:
        steps_by_phase.update({k: int(v) for k, v in (r.get("steps_by_phase") or {}).items()})
    return {
        "results": n,
        "verified_rate": round(sum(1 for r in runs if r.get("verified")) / n, 4),
        "loop_rate": round(sum(1 for r in runs if r.get("loop_interventions")) / n, 4),
        "intervention_rate": round(
            sum(
                1
                for r in runs
                if any(r.get(k) for k in ("loop_interventions", "forced_transitions", "nudges"))
            )
            / n,
            4,
        ),
        "avg_steps_after_green": _mean([int(r.get("steps_after_green", 0)) for r in runs]),
        "avg_initial_failures": _mean([int(r.get("initial_failures", 0)) for r in runs]),
        "steps_by_phase": dict(sorted(steps_by_phase.items())),
        **{k: sum(int(r.get(k, 0)) for r in runs) for k in RUNTIME_COUNTERS},
    }


def format_agent_metrics(metrics: dict[str, Any]) -> str:
    m = metrics
    lines = [
        f"agent: success {m['success_rate']:.1%} · patch rate {m['patch_rate']:.1%} · "
        f"regressions {m['regression_rate']:.1%}",
        f"  steps {m['avg_steps']:.1f} · tool calls {m['avg_tool_calls']:.1f} "
        f"(invalid {m['invalid_tool_call_rate']:.1%}) · test runs {m['avg_test_runs']:.1f}",
        f"  tokens median {m['tokens_median']:,} · cost median ${m['cost_median_usd']:.3f} "
        f"total ${m['cost_total_usd']:.2f} · solve p50 {m['solve_p50_seconds']:.0f}s "
        f"p95 {m['solve_p95_seconds']:.0f}s",
        "  terminations: "
        + ", ".join(f"{k} {v}" for k, v in sorted(metrics["terminations"].items())),
    ]
    if metrics["failures"]:
        lines.append("  failures: " + ", ".join(f"{k} {v}" for k, v in metrics["failures"].items()))
    runtime = metrics.get("runtime")
    if runtime:
        lines.append(
            f"  runtime: verified {runtime['verified_rate']:.1%} · loop rate "
            f"{runtime['loop_rate']:.1%} · interventions: loop {runtime['loop_interventions']}, "
            f"forced {runtime['forced_transitions']}, nudges {runtime['nudges']}, refused "
            f"{runtime['refused_tool_calls']}, resets {runtime['workspace_resets']} · "
            f"steps after green {runtime['avg_steps_after_green']:.1f}"
        )
        lines.append(
            "  steps by phase: "
            + ", ".join(f"{k} {v}" for k, v in runtime["steps_by_phase"].items())
        )
    for label, buckets in (
        ("category", metrics["by_category"]),
        ("difficulty", metrics["by_difficulty"]),
    ):
        if buckets:
            lines.append(
                f"  by {label}: "
                + ", ".join(f"{k} {v['pass']}/{v['total']}" for k, v in buckets.items())
            )
    return "\n".join(lines)
