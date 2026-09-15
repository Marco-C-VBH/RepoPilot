"""One task, end to end: build image -> solve -> evaluate in a fresh sandbox -> record.

``evaluate_patch`` is the only place a verdict is produced.  It always starts a
new container from the task image, applies the candidate patch, then the hidden
regression tests, runs the task's test command under the task's timeout, and
hands the ``TestRun`` to ``judge``.  Patch-application failures and packaging
problems are turned into explicit reason codes instead of exceptions so a
benchmark run never aborts halfway.
"""

from __future__ import annotations

import shlex
import time
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from evals.benchmark.schema import Task, touched_files
from evals.judge import Reason, Status, Verdict, judge
from evals.solvers import Solver
from repopilot.agent.baseline import AgentRun
from repopilot.sandbox.docker import (
    DockerError,
    ImageBuildError,
    ImageSpec,
    Sandbox,
    build_task_image,
)
from repopilot.sandbox.limits import DEFAULT_LIMITS, SandboxLimits
from repopilot.sandbox.repo import DEFAULT_CACHE_DIR, RepoError
from repopilot.sandbox.results import TestRun
from repopilot.tools.paths import is_test_path

_DETAIL_LIMIT = 4000


def image_spec_for(task: Task) -> ImageSpec:
    return ImageSpec(
        repo=task.repo,
        base_commit=task.base_commit,
        python=task.env.python,
        install=task.env.install,
        bug_patch=task.bug_patch,
    )


def with_hidden_files(test_command: str, hidden_files: Sequence[str]) -> str:
    """Append hidden test files to a pytest command so they are always collected.

    pytest collects a path given twice only once, so ``pytest tests`` plus
    ``tests/test_x.py`` is harmless; a command that targets specific files
    still picks up the hidden ones.
    """
    extra = " ".join(shlex.quote(path) for path in hidden_files)
    return f"{test_command} {extra}".rstrip()


def evaluation_test_command(task: Task) -> str:
    """The task's test command plus its hidden test files.

    ``test_command`` stays usable inside the agent's workspace, where the
    hidden files do not exist; only the evaluation run sees them.
    """
    return with_hidden_files(task.test_command, task.hidden_test_files)


def split_patch(patch: str) -> list[tuple[str, str]]:
    """A git-format patch -> ``(path, block)`` per ``diff --git`` section."""
    blocks: list[str] = []
    current: list[str] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            blocks.append("".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append("".join(current))
    return [(next(iter(sorted(touched_files(b))), ""), b) for b in blocks]


def strip_test_files(patch: str) -> tuple[str, list[str]]:
    """Drop the sections of a candidate patch that touch test files.

    The benchmark judges a fix with the task's own tests, so an edit to the
    suite can only hide a bug; it is removed before judging and reported in
    ``TaskResult.patch_test_files`` (the tool layer refuses such edits too).
    """
    kept: list[str] = []
    dropped: list[str] = []
    for path, block in split_patch(patch):
        if path and is_test_path(path):
            dropped.append(path)
        else:
            kept.append(block)
    return "".join(kept), sorted(set(dropped))


@dataclass(frozen=True)
class Evaluation:
    status: Status
    reasons: tuple[Reason, ...]
    detail: str | None
    verdict: Verdict | None
    test_run: TestRun | None
    patch_applied: bool | None
    hidden_tests_applied: bool | None
    duration_seconds: float
    patch_test_files: tuple[str, ...] = ()


def evaluate_patch(
    task: Task,
    image: str,
    patch: str | None,
    *,
    limits: SandboxLimits = DEFAULT_LIMITS,
) -> Evaluation:
    """Judge ``patch`` for ``task`` in a fresh container built from ``image``."""
    started = time.monotonic()
    stripped: list[str] = []
    if patch:
        patch, stripped = strip_test_files(patch)
        if not patch.strip():
            patch = None

    def done(
        status: Status,
        reasons: tuple[Reason, ...],
        *,
        detail: str | None = None,
        verdict: Verdict | None = None,
        test_run: TestRun | None = None,
        patch_applied: bool | None = None,
        hidden_tests_applied: bool | None = None,
    ) -> Evaluation:
        return Evaluation(
            status=status,
            reasons=reasons,
            detail=detail,
            verdict=verdict,
            test_run=test_run,
            patch_applied=patch_applied,
            hidden_tests_applied=hidden_tests_applied,
            duration_seconds=time.monotonic() - started,
            patch_test_files=tuple(stripped),
        )

    with Sandbox(image, limits) as sandbox:
        patch_applied: bool | None = None
        if patch:
            applied = sandbox.apply_patch(patch)
            patch_applied = applied.ok
            if not applied.ok:
                return done(
                    Status.FAIL,
                    (Reason.PATCH_APPLY_FAILED,),
                    detail=_clip(applied.stderr or applied.stdout),
                    patch_applied=False,
                )

        hidden_applied: bool | None = None
        if task.hidden_test_patch:
            applied = sandbox.apply_patch(task.hidden_test_patch)
            hidden_applied = applied.ok
            if not applied.ok:
                # Without a candidate patch this can only be a packaging problem.
                if patch:
                    status, reason = Status.FAIL, Reason.HIDDEN_TESTS_CONFLICT
                else:
                    status, reason = Status.ERROR, Reason.HIDDEN_TESTS_BROKEN
                return done(
                    status,
                    (reason,),
                    detail=_clip(applied.stderr or applied.stdout),
                    patch_applied=patch_applied,
                    hidden_tests_applied=False,
                )

        run = sandbox.run_tests(
            evaluation_test_command(task), timeout=task.env.test_timeout_seconds
        )

    verdict = judge(run, task.fail_to_pass, task.pass_to_pass)
    return done(
        verdict.status,
        verdict.reasons,
        verdict=verdict,
        test_run=run,
        patch_applied=patch_applied,
        hidden_tests_applied=hidden_applied,
    )


class TaskResult(BaseModel):
    """One line of results.jsonl: everything needed to aggregate and to debug."""

    task_id: str
    solver: str
    repeat: int = 0
    status: Status
    reasons: list[Reason] = Field(default_factory=list)
    detail: str | None = None
    image: str | None = None
    patch_bytes: int = 0
    patch_applied: bool | None = None
    hidden_tests_applied: bool | None = None
    fail_to_pass: dict[str, str | None] = Field(default_factory=dict)
    pass_to_pass: dict[str, str | None] = Field(default_factory=dict)
    test_counts: dict[str, int] = Field(default_factory=dict)
    test_exit_code: int | None = None
    timed_out: bool = False
    durations: dict[str, float] = Field(default_factory=dict)
    started_at: str
    patch_test_files: list[str] = Field(default_factory=list)  # test edits stripped before judging
    agent: dict[str, Any] | None = None  # AgentRun.to_record() when the solver is an agent
    trace_path: str | None = None
    failure: str | None = None  # evals.metrics.Failure, set by the runner

    @property
    def reason(self) -> str:
        return "+".join(self.reasons) if self.reasons else "-"

    def signature(self) -> tuple[object, ...]:
        """What must be identical between repeated runs for the task to count as deterministic."""
        return (
            str(self.status),
            tuple(str(r) for r in self.reasons),
            tuple(sorted(self.fail_to_pass.items())),
            tuple(sorted(self.pass_to_pass.items())),
        )


def run_task(
    task: Task,
    solver: Solver,
    *,
    limits: SandboxLimits = DEFAULT_LIMITS,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    rebuild: bool = False,
    repeat: int = 0,
    trace_dir: Path | None = None,
) -> tuple[TaskResult, str]:
    """Build, solve, evaluate.  Returns the result record and a human-readable log.

    A solver that exposes ``last_run`` (an ``AgentRun``) gets its summary stored
    in ``TaskResult.agent`` and its trace written to ``trace_dir``.
    """
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    t0 = time.monotonic()
    durations: dict[str, float] = {}

    def finish(result: TaskResult, log: list[str]) -> tuple[TaskResult, str]:
        durations["total"] = round(time.monotonic() - t0, 3)
        result.durations = durations
        return result, "\n".join(log) + "\n"

    base = dict(task_id=task.id, solver=solver.name, repeat=repeat, started_at=started_at)
    log = [f"# {task.id} · solver={solver.name} · repeat={repeat} · {started_at}", ""]

    try:
        image = build_task_image(image_spec_for(task), cache_dir=cache_dir, rebuild=rebuild)
    except (ImageBuildError, RepoError, DockerError) as exc:
        durations["build"] = round(time.monotonic() - t0, 3)
        detail = _clip(str(exc))
        log += ["## image build FAILED", detail]
        return finish(
            TaskResult(
                **base, status=Status.ERROR, reasons=[Reason.IMAGE_BUILD_FAILED], detail=detail
            ),
            log,
        )
    durations["build"] = round(time.monotonic() - t0, 3)
    log.append(f"image: {image}  (build {durations['build']:.1f}s)")

    t1 = time.monotonic()
    try:
        patch = solver.solve(task, image)
    except Exception:  # a solver bug must not take the benchmark down
        durations["solve"] = round(time.monotonic() - t1, 3)
        detail = _clip(traceback.format_exc())
        log += ["## solver CRASHED", detail]
        return finish(
            TaskResult(
                **base,
                status=Status.ERROR,
                reasons=[Reason.SOLVER_CRASHED],
                detail=detail,
                image=image,
            ),
            log,
        )
    durations["solve"] = round(time.monotonic() - t1, 3)
    patch_bytes = len(patch.encode("utf-8")) if patch else 0
    log += [f"solve: {durations['solve']:.1f}s, patch {patch_bytes} bytes", ""]

    agent_record: dict[str, Any] | None = None
    trace_path: str | None = None
    agent_run = getattr(solver, "last_run", None)
    if isinstance(agent_run, AgentRun):
        agent_record = agent_run.to_record()
        if trace_dir is not None:
            suffix = f".{repeat}" if repeat else ""
            trace_path = str(agent_run.trace.write(trace_dir / f"{task.id}{suffix}.jsonl"))
        log += [
            "## agent",
            f"termination: {agent_run.termination}"
            + (f" ({agent_run.detail})" if agent_run.detail else ""),
            f"steps {agent_run.steps} · tool calls {agent_run.tool_calls} "
            f"(invalid {agent_run.invalid_tool_calls}) · test runs {agent_run.test_runs} "
            f"(refused {agent_run.test_runs_refused}) · {agent_run.elapsed_seconds:.0f}s",
            f"tokens {agent_run.ledger['input_tokens']} in / "
            f"{agent_run.ledger['output_tokens']} out · ${agent_run.ledger['cost_usd']:.4f}",
            f"files read: {', '.join(agent_run.files_read) or '-'}",
            f"files edited: {', '.join(agent_run.files_edited) or '-'}",
        ]
        if agent_run.runtime:
            rt = agent_run.runtime
            log += [
                "phases: "
                + " -> ".join(str(t["to"]) for t in rt.get("transitions", []))
                + f" · verified {rt.get('verified')}",
                f"interventions: loop {rt.get('loop_interventions', 0)} · forced "
                f"{rt.get('forced_transitions', 0)} · nudges {rt.get('nudges', 0)} · refused "
                f"{rt.get('refused_tool_calls', 0)} · resets {rt.get('workspace_resets', 0)}",
            ]
        log += [
            f"final: {(agent_run.final_text or '').strip()[:600]}",
            f"trace: {trace_path or '(not written)'}",
            "",
        ]
    log += ["## candidate patch", patch.rstrip() if patch else "(none)", ""]

    t2 = time.monotonic()
    try:
        evaluation = evaluate_patch(task, image, patch, limits=limits)
    except DockerError as exc:
        durations["evaluate"] = round(time.monotonic() - t2, 3)
        detail = _clip(str(exc))
        log += ["## sandbox ERROR", detail]
        return finish(
            TaskResult(
                **base,
                status=Status.ERROR,
                reasons=[Reason.SANDBOX_ERROR],
                detail=detail,
                image=image,
                patch_bytes=patch_bytes,
                agent=agent_record,
                trace_path=trace_path,
            ),
            log,
        )
    durations["evaluate"] = round(evaluation.duration_seconds, 3)

    run = evaluation.test_run
    verdict = evaluation.verdict
    result = TaskResult(
        **base,
        status=evaluation.status,
        reasons=list(evaluation.reasons),
        detail=evaluation.detail,
        image=image,
        patch_bytes=patch_bytes,
        patch_applied=evaluation.patch_applied,
        hidden_tests_applied=evaluation.hidden_tests_applied,
        fail_to_pass=verdict.fail_to_pass if verdict else {},
        pass_to_pass=verdict.pass_to_pass if verdict else {},
        test_counts=run.counts() if run else {},
        test_exit_code=run.exit_code if run else None,
        timed_out=run.timed_out if run else False,
        patch_test_files=list(evaluation.patch_test_files),
        agent=agent_record,
        trace_path=trace_path,
    )

    log += [f"## verdict: {result.status}  reasons: {result.reason}"]
    if evaluation.patch_test_files:
        log.append(
            f"test-file edits stripped before judging: {', '.join(evaluation.patch_test_files)}"
        )
    if evaluation.detail:
        log += [evaluation.detail, ""]
    if verdict:
        log.append("fail_to_pass:")
        log += [
            f"  {'ok ' if o == 'passed' else 'XX '}{t}: {o}"
            for t, o in verdict.fail_to_pass.items()
        ]
        log.append("pass_to_pass:")
        log += [
            f"  {'ok ' if o == 'passed' else 'XX '}{t}: {o}"
            for t, o in verdict.pass_to_pass.items()
        ]
    if run:
        log += [
            "",
            f"## test run: {run.summary()}",
            f"$ {run.command}",
            "",
            "### stdout",
            run.stdout,
        ]
        if run.stderr.strip():
            log += ["", "### stderr", run.stderr]
    return finish(result, log)


def _clip(text: str, limit: int = _DETAIL_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else "...\n" + text[-limit:]
