# RepoPilot

An evaluation-driven repository debugging agent for Python projects. Given a Git
repository and a bug report, RepoPilot searches the codebase, inspects the relevant
code, proposes a patch, validates it with pytest inside an isolated Docker sandbox,
and iterates on failures under a fixed step / token / cost / runtime budget — while
recording a structured trace of everything it did.

The point is not the LLM call. The point is measurement: task success on a
deterministic benchmark, retrieval recall, cost, latency, and a failure taxonomy,
with every added feature justified by an ablation.

**Status: Phase 0 (evaluation harness) — in progress.** No benchmark numbers yet.
This README will open with measured results once the harness and the baseline
agent exist; nothing here will be a placeholder number.

## Setup

```bash
uv sync                                   # creates .venv and installs dev tools
uv run pytest                             # schema / registry / limits tests
uv run ruff check . && uv run ruff format --check .
uv run python scripts/validate_tasks.py   # validate every task in evals/benchmark/tasks
uv run python -m evals.runner --list      # tasks that a benchmark run would select
```

Docker is required from Phase 0 step 2 onward (sandboxed test execution).

## Layout

```
repopilot/                 library
  sandbox/limits.py        resource limits applied to every sandbox container
  sandbox/docker.py        image build + throwaway test containers  (TODO, step 2)
evals/                     RepoPilot-Bench
  benchmark/schema.py      Task model — the source of truth for task files
  benchmark/registry.py    loads and validates evals/benchmark/tasks/<id>.json
  benchmark/task.schema.json   exported JSON Schema (scripts/export_task_schema.py)
  benchmark/examples/      a fully filled-in illustrative task (not runnable)
  benchmark/tasks/         the benchmark itself, one JSON file per task
  runner.py                benchmark runner CLI                     (TODO, step 3)
docker/base.Dockerfile     base image for sandbox containers
scripts/                   validate_tasks.py, export_task_schema.py
tests/                     unit tests for the harness itself
```

## Task format

A task is one reproducible debugging problem: a repository pinned to a full commit
SHA, the bug report the agent sees, the reference fix, and the tests that decide
success. Success is deterministic — after the candidate patch (plus the hidden test
patch) is applied, every `fail_to_pass` test must pass **and** every `pass_to_pass`
test must still pass. There is no LLM judge.

Two task sources: `real` (the bug already exists at `base_commit`) and `mutation`
(`base_commit` is clean and the harness injects the bug with `bug_patch` before the
agent sees the repository). See `evals/benchmark/schema.py` for every field and the
invariants the loader enforces, and `evals/benchmark/examples/example_000.json` for
a complete example.

## Phase 0 checklist

- [x] Repository skeleton, `pyproject.toml`, task schema + registry + unit tests
- [ ] Docker sandbox: per-task image build, throwaway container, `--network none`,
      CPU/memory/pids/time limits, junit parsing
- [ ] Runner with `null` and `gold` oracle solvers — `null` → 0/N, `gold` → N/N,
      and a second run gives identical per-task results
- [ ] 10–20 controlled-mutation tasks on 2–3 small, pure-Python, fast-testing repos

Phase 1 (baseline agent) starts only after the checklist above is green.
