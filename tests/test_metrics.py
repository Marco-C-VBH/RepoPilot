"""Failure taxonomy, agent metric aggregation and the harness's test-edit stripping."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.benchmark.schema import Task
from evals.harness import TaskResult, split_patch, strip_test_files
from evals.judge import Reason, Status
from evals.metrics import Failure, agent_metrics, classify_failure, format_agent_metrics

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "evals" / "benchmark" / "examples" / "example_000.json"


@pytest.fixture(scope="module")
def task() -> Task:
    return Task.model_validate(json.loads(EXAMPLE.read_text(encoding="utf-8")))


def result(
    task: Task,
    status: Status,
    *,
    reasons: list[Reason] | None = None,
    agent: dict | None = None,
    patch_bytes: int = 0,
    patch_test_files: list[str] | None = None,
    solve: float = 10.0,
) -> TaskResult:
    return TaskResult(
        task_id=task.id,
        solver="baseline",
        status=status,
        reasons=reasons or [],
        started_at="2026-09-14T00:00:00+00:00",
        durations={"total": solve + 5, "solve": solve},
        patch_bytes=patch_bytes,
        patch_test_files=patch_test_files or [],
        agent=agent,
    )


def agent(
    termination: str = "done",
    *,
    read: list[str] | None = None,
    edited: list[str] | None = None,
    steps: int = 5,
    tool_calls: int = 4,
    invalid: int = 0,
    cost: float = 0.05,
    tokens: tuple[int, int] = (20_000, 1_000),
) -> dict:
    return {
        "termination": termination,
        "steps": steps,
        "tool_calls": tool_calls,
        "invalid_tool_calls": invalid,
        "test_runs": 1,
        "cost_usd": cost,
        "input_tokens": tokens[0],
        "output_tokens": tokens[1],
        "files_read": read or [],
        "files_edited": edited or [],
        "changed_files": edited or [],
    }


def test_classification_covers_the_taxonomy(task: Task) -> None:
    gold = task.gold_files[0]
    assert classify_failure(task, result(task, Status.PASS)) is None
    assert classify_failure(task, result(task, Status.ERROR)) is Failure.ENVIRONMENT_FAILURE
    fail = Status.FAIL
    f2p = [Reason.FAIL_TO_PASS_FAILING]
    cases = [
        (agent("model_error"), f2p, [], Failure.ENVIRONMENT_FAILURE),
        (agent(), [Reason.PATCH_APPLY_FAILED], [], Failure.TOOL_FAILURE),
        (agent(edited=[gold]), [Reason.PASS_TO_PASS_REGRESSED], [], Failure.REGRESSION_INTRODUCED),
        (agent("budget_steps", read=[gold]), f2p, [], Failure.BUDGET_EXCEEDED),
        (agent(read=["README.md"]), f2p, [], Failure.RETRIEVAL_FAILURE),
        (agent(read=[gold]), f2p, [], Failure.REASONING_FAILURE),
        (agent(read=[gold], edited=["src/other.py"]), f2p, [], Failure.WRONG_LOCALIZATION),
        (agent(read=["x.py"], edited=["src/other.py"]), f2p, [], Failure.RETRIEVAL_FAILURE),
        (agent(read=[gold], edited=[gold]), f2p, [], Failure.INCORRECT_PATCH),
        (
            agent(read=[gold], edited=["tests/test_x.py"]),
            f2p,
            ["tests/test_x.py"],
            Failure.TEST_MISUNDERSTANDING,
        ),
    ]
    for record, reasons, stripped, expected in cases:
        r = result(task, fail, reasons=reasons, agent=record, patch_test_files=stripped)
        assert classify_failure(task, r) is expected, (record, reasons, expected)


def test_classification_without_an_agent_record(task: Task) -> None:
    r = result(task, Status.FAIL, reasons=[Reason.FAIL_TO_PASS_FAILING])
    assert classify_failure(task, r) is Failure.RETRIEVAL_FAILURE  # nothing read, nothing edited


def test_agent_metrics_aggregate_and_format(task: Task) -> None:
    gold = task.gold_files[0]
    results = [
        result(task, Status.PASS, agent=agent(edited=[gold], cost=0.10), patch_bytes=120, solve=30),
        result(
            task,
            Status.FAIL,
            reasons=[Reason.FAIL_TO_PASS_FAILING],
            agent=agent("budget_cost", read=[gold], steps=30, tool_calls=40, invalid=4, cost=0.50),
            patch_bytes=0,
            solve=90,
        ),
        result(
            task,
            Status.FAIL,
            reasons=[Reason.PASS_TO_PASS_REGRESSED],
            agent=agent(edited=[gold], cost=0.20),
            patch_bytes=80,
            solve=45,
        ),
        result(task, Status.PASS),  # no agent record (e.g. gold solver) -> ignored
    ]
    metrics = agent_metrics([task], results)
    assert metrics is not None
    assert metrics["results"] == 3
    assert metrics["success_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert metrics["patch_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert metrics["regression_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert metrics["avg_steps"] == pytest.approx((5 + 30 + 5) / 3, abs=1e-3)
    assert metrics["invalid_tool_call_rate"] == pytest.approx(4 / 48, abs=1e-4)
    assert metrics["terminations"] == {"done": 2, "budget_cost": 1}
    assert metrics["cost_total_usd"] == pytest.approx(0.80)
    assert metrics["cost_median_usd"] == pytest.approx(0.20)
    assert metrics["tokens_median"] == 21_000
    assert metrics["solve_p50_seconds"] == 45.0 and metrics["solve_p95_seconds"] == 90.0
    assert metrics["failures"] == {"budget_exceeded": 1, "regression_introduced": 1}
    assert metrics["by_category"] == {str(task.category): {"pass": 1, "total": 3}}
    assert metrics["by_difficulty"] == {str(task.difficulty): {"pass": 1, "total": 3}}

    text = format_agent_metrics(metrics)
    assert "success 33.3%" in text and "terminations: budget_cost 1, done 2" in text
    assert "failures: budget_exceeded 1, regression_introduced 1" in text
    assert agent_metrics([task], [result(task, Status.PASS)]) is None


PATCH = """\
diff --git a/src/pkg/core.py b/src/pkg/core.py
--- a/src/pkg/core.py
+++ b/src/pkg/core.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
 y = 3
diff --git a/tests/test_core.py b/tests/test_core.py
--- a/tests/test_core.py
+++ b/tests/test_core.py
@@ -1,2 +1,2 @@
-assert x == 1
+assert x == 2
 pass
"""


def test_split_and_strip_test_files() -> None:
    blocks = split_patch(PATCH)
    assert [path for path, _ in blocks] == ["src/pkg/core.py", "tests/test_core.py"]
    kept, dropped = strip_test_files(PATCH)
    assert dropped == ["tests/test_core.py"]
    assert kept.startswith("diff --git a/src/pkg/core.py") and "tests/test_core.py" not in kept
    assert strip_test_files("") == ("", [])
    only_tests = PATCH[PATCH.index("diff --git a/tests") :]
    assert strip_test_files(only_tests) == ("", ["tests/test_core.py"])
