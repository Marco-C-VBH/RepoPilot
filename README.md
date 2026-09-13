# RepoPilot

An evaluation-driven repository debugging agent for Python projects. Given a Git
repository and a bug report, RepoPilot searches the codebase, inspects the relevant
code, proposes a patch, validates it with pytest inside an isolated Docker sandbox,
and iterates on failures under a fixed step / token / cost / runtime budget — while
recording a structured trace of everything it did.

The point is not the LLM call. The point is measurement: task success on a
deterministic benchmark, retrieval recall, cost, latency, and a failure taxonomy,
with every added feature justified by an ablation.

**Status: Phase 0 (evaluation harness) — done; Phase 1 (baseline agent) next.**
RepoPilot-Bench v0 has 14 deterministic tasks and the harness passes its own
acceptance test (see [the benchmark](#repopilot-bench-v0)). There are no agent
results yet: this README will open with measured results once the baseline agent
exists, and nothing here will be a placeholder number.

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

## Running the benchmark

```bash
uv run python -m evals.runner --solver null --expect fail            # untouched: all FAIL
uv run python -m evals.runner --solver gold --expect pass --repeat 2 # reference fix: all PASS, twice
uv run python -m evals.runner --solver gold --ids cachetools_001     # a subset
```

Every run writes `results/<solver>-<timestamp>-<id>/` with `results.jsonl` (one
record per task and repeat: status, reason codes, per-test outcomes, timings),
`summary.json` (counts, pass rate, determinism verdict) and `logs/<task>.log`
(the candidate patch, the test output, the per-test verdict table). The two
commands above are the harness's own acceptance test: a task that the null solver
passes or the gold solver fails is mis-packaged, and a task whose outcome differs
between repeats is flaky.

A solver is anything with `solve(task, image) -> patch | None`; the harness always
judges the returned patch in a fresh container, so a solver's own workspace can
never influence the verdict. The Phase 1 baseline agent will be one more entry in
`evals/solvers.py`.

## RepoPilot-Bench v0

14 controlled-mutation tasks on three small, pure-Python libraries pinned to one
commit each (cachetools 7.1.8, toolz 1.1.0, tenacity 9.2.0). Every task is a
small bug injected into the library (at most six changed lines in the diff), a
bug report written from the outside
(what a user would observe, never the fix), and a hidden regression test that the
agent never sees. `fail_to_pass` lists the tests a fix must turn green — the
hidden ones plus whatever existing tests the bug already breaks — and
`pass_to_pass` is everything else the task's test command collects.

| id | category | difficulty | mutation site | fail_to_pass (visible + hidden) | pass_to_pass |
| --- | --- | --- | --- | --- | --- |
| cachetools_001 | cache_invalidation | medium | `TTLCache.expire` | 2 + 2 | 56 |
| cachetools_002 | cache_invalidation | hard | `TLRUCache.__setitem__` | 1 + 2 | 64 |
| cachetools_003 | state_management | medium | `LRUCache.__getitem__` | 2 + 2 | 52 |
| cachetools_004 | off_by_one | easy | `Cache.__setitem__` | 28 + 2 | 22 |
| cachetools_005 | wrong_condition | medium | `cached()` wrapper | 20 + 2 | 54 |
| tenacity_001 | retry_logic | easy | `stop_after_attempt.__call__` | 23 + 2 | 112 |
| tenacity_002 | retry_logic | medium | `wait_exponential.__call__` | 8 + 2 | 127 |
| tenacity_003 | exception_handling | medium | `retry_if_exception_type._check` | 12 + 2 | 123 |
| tenacity_004 | exception_handling | hard | `RetryError.reraise` | 1 + 2 | 134 |
| tenacity_005 | missing_check | medium | `wait_chain.__call__` | 2 + 2 | 133 |
| toolz_001 | off_by_one | easy | `sliding_window` | 1 + 3 | 59 |
| toolz_002 | wrong_condition | medium | `unique` | 1 + 3 | 59 |
| toolz_003 | missing_check | medium | `dissoc` | 0 + 3 | 57 |
| toolz_004 | state_management | hard | `memoize` | 0 + 3 | 48 |

Seven categories, each twice; 3 easy / 8 medium / 3 hard. Twelve tasks have at
least one existing test that fails with the bug, so an agent can reproduce them by
running the suite; two (`toolz_003`, `toolz_004`) are caught only by the hidden
test and must be reproduced from the description. Difficulty is set by how much
the description gives away, not by patch size — every gold patch is tiny.

**Harness acceptance (2026-09-10, MacBook Air / Docker Desktop, one container
per run):**

| run | result | wall time |
| --- | --- | --- |
| `--solver null --expect fail` | 0 / 14 pass, every failure `fail_to_pass_failing` | 21 s |
| `--solver gold --expect pass --repeat 2` | 28 / 28 pass, per-test outcomes identical across repeats (14 / 14) | 38 s |

These numbers describe the harness, not an agent: a null solver must fail every
task, the reference fix must pass every task, and nothing may be flaky. The
first agent numbers will come from Phase 1.

The audit trail for every task is its source directory
(`evals/benchmark/sources/<id>/`), and `tests/test_benchmark_tasks.py` fails the
unit suite if a task JSON ever drifts from its sources.

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
  benchmark/sources/<id>/  human-written task sources: task.toml, bug.patch, hidden.patch,
                           description.md (docs/benchmark-authoring.md)
  benchmark/authoring.py   source dir -> derived gold patch, symbols, f2p/p2p -> task JSON
  benchmark/tasks/         the benchmark itself, one JSON file per task
  judge.py                 fail_to_pass ∧ pass_to_pass -> Verdict with reason codes
  solvers.py               Solver protocol; `null` and `gold` oracles
  harness.py               build image -> solve -> evaluate in a fresh sandbox -> TaskResult
  runner.py                CLI: results.jsonl, summary.json, per-task logs, --repeat, --expect
docker/base.Dockerfile     base image for sandbox containers
docs/benchmark-authoring.md  how tasks are made: target repos, workflow, rules, coverage plan
docs/issues.md             engineering log: symptom -> root cause -> fix -> guard
scripts/                   make_task.py, validate_tasks.py, export_task_schema.py
tests/                     unit tests (fast) + `-m docker` end-to-end tests;
                           test_benchmark_tasks.py checks the shipped tasks against their sources
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

Tasks are not written by hand. A source directory holds the human parts — the
mutation as a patch, a hidden regression test, the bug report, a few lines of
metadata — and `scripts/make_task.py` derives the rest by running the code: the
gold patch is the exact reverse of the mutation, the localization targets come from
the patch hunks and the AST, and `fail_to_pass` / `pass_to_pass` come from two
sandbox runs (buggy vs. fixed, both with the hidden tests). A task whose hidden
test does not catch the bug, or whose fix breaks an existing test, is refused. See
`docs/benchmark-authoring.md`.

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
- [x] Runner with `null` and `gold` oracle solvers, `--expect` and `--repeat`
      (`null` → 0/N, `gold` → N/N, repeated runs identical) — verified end to end
      on a fixture task by `uv run pytest -m docker`
- [x] Task authoring pipeline (`scripts/make_task.py`, `docs/benchmark-authoring.md`):
      derived gold patch / symbols / test sets, packaging mistakes rejected up front
- [x] 10–20 controlled-mutation tasks on cachetools, toolz and tenacity (pinned
      commits in `docs/benchmark-authoring.md`): 14 tasks, `null → 0/14`,
      `gold → 28/28` over two repeats with identical per-test outcomes (2026-09-10)

Phase 0 is complete. Phase 1 (baseline agent: a deliberately simple
read / search / patch / test loop, registered as a solver and run on the 14 tasks
to produce the first real success-rate, cost and latency numbers) is next.
