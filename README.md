# RepoPilot

An evaluation-driven repository debugging agent for Python projects. Given a Git
repository and a bug report, RepoPilot searches the codebase, inspects the relevant
code, proposes a patch, validates it with pytest inside an isolated Docker sandbox,
and iterates on failures under a fixed step / token / cost / runtime budget — while
recording a structured trace of everything it did.

The point is not the LLM call. The point is measurement: task success on a
deterministic benchmark, retrieval recall, cost, latency, and a failure taxonomy,
with every added feature justified by an ablation.

**Status: Phase 0 (evaluation harness) — done; Phase 1 (baseline agent) — done,
first results below.** Every number here is measured by a run archived under
`evals/experiments/`; nothing is a placeholder.

## Results so far

| configuration | benchmark | success | median cost / task | median time | steps |
| --- | --- | --- | --- | --- | --- |
| baseline: `claude-sonnet-5`, plain tool loop, spec §9.1 budget | RepoPilot-Bench v0 (14 tasks) | **14 / 14** | $0.045 | 16 s | 6.2 |

Single run, 2026-09-14 (`evals/experiments/baseline-v0-sonnet5/`): 100% success,
100% patch rate, 0% regressions, $0.79 for the whole run, invalid tool-call rate
1.3%, p95 solve time 23 s, every run ended with the model stopping on its own —
no task came near the budget (max context 9.5k tokens, max spend $0.13).

That is the first finding of Phase 1, and it is about the benchmark, not the
agent: **v0 is saturated.** Its tasks are single-site mutations in small
libraries, and even the "hard" bug reports carry the name of the class or
decorator involved, so `search_symbol` on a name from the report lands on the
right function in one call (traces: the first tool call was a symbol lookup or a
direct read in 14 / 14 runs; the two hidden-only tasks passed without the agent
ever seeing a failing test). A benchmark the baseline completes cannot show what
structured runtime or hybrid retrieval add, so the next work is v1: bug reports
audited for leaked identifiers, larger repositories, mutations whose symptom
surfaces in a different module than the cause, and cheaper models as a second
axis (see [Phase 1 findings](#phase-1-findings)).

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

## Models and API keys

The agent talks to models through one small interface
(`repopilot/models/`): neutral `Message` / `ToolSpec` / `ModelResponse` types,
an adapter per vendor (Anthropic Messages API, OpenAI Responses API), a
`FakeClient` that plays scripted replies for tests, a price table, and a
`Ledger` that turns every call into tokens, latency and an estimated cost and
enforces the cost / token caps of the agent budget (spec §9.1). Two model roles
follow the routing ablation (§12.4): a *strong* model for planning and patching
and a *cheap* one for rewriting and summarizing.

| role | default | override |
| --- | --- | --- |
| strong | `claude-sonnet-5` ($2 / $10 per MTok in / out) | `REPOPILOT_STRONG_MODEL` or a CLI flag |
| cheap | `claude-haiku-4-5-20251001` ($1 / $5) | `REPOPILOT_CHEAP_MODEL` or a CLI flag |

OpenAI counterparts (`gpt-5.6-terra` $2 / $12, `gpt-5.6-luna` $0.20 / $1.20)
plug in by name; any model used must have an entry in
`repopilot/models/pricing.py`, so a cost is never unknown. Costs are estimates
from that table (dated inside the file), not from the vendors' billing pages,
which makes them reproducible from a trace alone.

Keys never appear in code, traces, logs or the sandbox (containers start with
a clean environment and no network). They come from the environment only:

```bash
cp .env.example .env                       # git-ignored; fill in ANTHROPIC_API_KEY / OPENAI_API_KEY
uv run python scripts/model_smoke.py       # one tiny text + tool-call round trip per model, ~$0.001
uv run pytest -m llm                       # the same as tests; skipped automatically without keys
```

CLI entry points load `.env`; library code only reads `os.environ`, and error
messages name the missing variable, never a value. CI runs with no keys and
deselects `-m llm`; `tests/test_no_secrets.py` fails the build if a key prefix
is ever committed. Use a dedicated key per provider with a spend limit set in
the vendor console, and revoke it if it leaks.

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
never influence the verdict. Any section of a candidate patch that touches a test
file is stripped before judging and recorded (`patch_test_files`): the benchmark's
own tests decide, so editing them can only hide a bug.

## The baseline agent (Phase 1)

`--solver baseline` runs `repopilot/agent/baseline.py`: a plain tool-calling loop
— system prompt, bug report, then *model → tools → results* until the model
stops or the budget is spent. No planning phase, no state machine, no loop
detection, no context compaction: it is the unconstrained end of the
architecture ablation (spec §12.2), and every later phase is measured against it.

```bash
uv run python -m evals.runner --solver baseline --ids cachetools_004 --max-run-cost 2   # one task
uv run python -m evals.runner --solver baseline --max-run-cost 10                       # all tasks
uv run python -m evals.runner --solver baseline --model gpt-5.6-terra --max-steps 20    # variations
```

The agent works on a host-side git checkout of the buggy tree (`repopilot/tools/`)
through six typed tools — `search_code`, `search_symbol`, `find_references`,
`read_file`, `edit_file`, `run_tests` — and only `run_tests` touches the sandbox:
the workspace diff is applied to a fresh copy of the tree in the container and
pytest runs there. The final diff is the candidate patch. Every task runs under
the same budget (spec §9.1: 30 model calls, 40 tool calls, 5 test runs, 100k
tokens, $0.50, 600 s by default; `--max-*` flags change it for a whole run), and
`--max-run-cost` caps the spend of the entire benchmark run.

Each task leaves a trace (`results/<run>/traces/<task>.jsonl`, spec §13.1): the
run's metadata, every model call with tokens / latency / cost, every tool call
with arguments, duration, result and errors, the final patch, the budget state and
the termination reason. `summary.json` adds an `agent` block (spec §11.1): success,
patch and regression rates, steps / tool calls / test runs, invalid-tool-call
rate, tokens, cost (median and total), solve latency p50 / p95, termination
counts, success by category and difficulty — and a first-cut failure taxonomy
(spec §11.2) derived from the verdict, the termination and whether the agent ever
read or edited a gold file: `retrieval_failure`, `reasoning_failure`,
`wrong_localization`, `incorrect_patch`, `regression_introduced`,
`budget_exceeded`, `test_misunderstanding`, `tool_failure`, `environment_failure`.
The taxonomy is heuristic and exists to point at the highest-leverage problem, not
to be ground truth.

### Phase 1 findings

What the 14 traces of the first run say (`evals/experiments/baseline-v0-sonnet5/traces/`):

- **Localization is free on v0.** 12 of 14 runs opened with `search_symbol(<name
  from the bug report>)`, the other two with a direct `read_file` / `search_code`
  on a term from the report; the first file read was a gold file in 13 / 14
  (`cachetools_005` read `__init__.py` first, then followed `cached` to the nested
  `_wrapper` in `_cached.py`: 5 reads across 3 files, 10 steps). The difficulty
  tiers only vary how much *behaviour* the report describes — the identifiers
  leak the location regardless.
- **Iteration barely happens.** 10 of 14 runs made exactly one edit and ran the
  tests once; three more only ran a narrower target first or split the fix into
  two edits. The one real correction loop was `cachetools_003`: the first edit
  broke `test_missing_getsizeof`, the agent read `Cache.__getitem__` and fixed it
  (11 steps, 2 test runs).
- **Budgets never bind.** Median 18.7k total tokens and $0.045; the most expensive
  run was 58.9k tokens / $0.13 (`cachetools_005`) and the slowest 36 s
  (`cachetools_003`), against caps of 100k / $0.50 / 600 s. 87% of solve time is
  model latency (~1.5 s per call); tool calls take milliseconds except `run_tests`
  (0.5–3 s).
- **The tool contract held.** One invalid call in 75 (`read_file` with
  `start="80, 130"`), rejected with a message the model recovered from on the next
  step; no test-file edits attempted; no patch failed to apply.

Consequences: success rate on v0 cannot discriminate agent designs, so the
Phase 2 / 3 comparisons need (1) **RepoPilot-Bench v1** — reports without leaked
identifiers, repositories an order of magnitude larger, cross-module symptoms,
more hidden-only tasks — and (2) a **second model tier** (`claude-haiku-4-5`,
`gpt-5.6-luna`) where scaffolding differences may show up at fixed model
strength. Efficiency (tokens, cost, latency) remains measurable on v0 either way.

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
  models/types.py          Message / ToolSpec / ToolCall / ModelResponse / Usage (vendor-neutral)
  models/client.py         AnthropicClient, OpenAIClient, FakeClient; client_for(model)
  models/config.py         ModelSettings (strong / cheap), key lookup, .env loading
  models/pricing.py        $/MTok table -> estimate_cost; models/ledger.py totals + budget caps
  tools/workspace.py       host-side git working copy of the buggy tree (edits live here)
  tools/code.py            read_file, search_code, search_symbol (ast index), find_references, edit_file
  tools/toolbox.py         the six tool schemas the model sees + dispatch/validation + run_tests
                           (workspace diff -> sandbox -> per-test summary)
  agent/budget.py          AgentBudget (spec §9.1) + BudgetTracker
  agent/prompts.py         system prompt and task prompt of the baseline
  agent/baseline.py        BaselineAgent: the budgeted tool loop -> AgentRun (metrics, patch, trace)
  tracing/events.py        Trace: JSONL events (model_call, tool_call, patch, run_end)
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
  solvers.py               Solver protocol; `null` and `gold` oracles; `baseline` agent solver
  harness.py               build image -> solve -> evaluate in a fresh sandbox -> TaskResult
                           (+ agent record, trace file, test-file edits stripped from patches)
  metrics.py               agent metrics (spec §11.1) and the failure taxonomy (spec §11.2)
  runner.py                CLI: results.jsonl, summary.json, logs/, traces/, --repeat, --expect,
                           --model and --max-* budget flags, --max-run-cost
docker/base.Dockerfile     base image for sandbox containers
docs/benchmark-authoring.md  how tasks are made: target repos, workflow, rules, coverage plan
docs/issues.md             engineering log: symptom -> root cause -> fix -> guard
evals/experiments/         archived runs behind the numbers in this README (summary, results, traces)
scripts/                   make_task.py, validate_tasks.py, export_task_schema.py, model_smoke.py,
                           archive_run.py (results/<run> -> evals/experiments/<name>)
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
