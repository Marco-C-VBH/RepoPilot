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
uv run pytest                             # unit tests; Docker tests skip if no daemon
uv run pytest -m docker                   # sandbox end-to-end tests (needs Docker running)
uv run ruff check . && uv run ruff format --check .
uv run python scripts/validate_tasks.py   # validate every task in evals/benchmark/tasks
uv run python -m evals.runner --list      # tasks that a benchmark run would select
```

Docker (Docker Desktop, OrbStack or colima) is required for sandboxed test
execution. The first `-m docker` run builds the base image (python:3.11-slim +
git + pytest), which takes a minute or two; later runs reuse it.

## Layout

```
repopilot/                 library
  sandbox/limits.py        resource limits applied to every sandbox container
  sandbox/repo.py          host-side checkout of a repo at a commit (mirror cache, no .git)
  sandbox/docker.py        per-task image build + Sandbox container (exec / apply_patch /
                           run_tests / diff), all through the docker CLI
  sandbox/results.py       ExecResult, TestRun, per-node-id TestOutcome
  sandbox/repopilot_pytest_plugin.py   loaded inside the container; exact pytest node
                           ids -> JSON report (no junit classname guessing)
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

## Sandbox

Each task gets a content-addressed Docker image (`repopilot-task:<hash>`): the
repository tree at `base_commit` is exported on the host, copied into the image,
turned into a single-commit git history (nothing to leak through `git log`), the
install command runs, and for mutation tasks `bug_patch` is applied and amended into
that one commit. Network is on during the build only. Every run then starts a fresh
container with `--network none`, CPU / memory / pids limits, all capabilities
dropped and an unprivileged user; `Sandbox.run_tests` loads a small pytest plugin
that writes exact node-id outcomes to JSON, so a missing node id is always
"not passed", never a parsing accident.

## Phase 0 checklist

- [x] Repository skeleton, `pyproject.toml`, task schema + registry + unit tests
- [x] Docker sandbox: per-task image build, throwaway container, `--network none`,
      CPU/memory/pids/time limits, per-test outcomes (`uv run pytest -m docker`)
- [ ] Runner with `null` and `gold` oracle solvers — `null` → 0/N, `gold` → N/N,
      and a second run gives identical per-task results
- [ ] 10–20 controlled-mutation tasks on 2–3 small, pure-Python, fast-testing repos

Phase 1 (baseline agent) starts only after the checklist above is green.
