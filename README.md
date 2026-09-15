# RepoPilot

An evaluation-driven repository debugging agent for Python projects. Given a Git
repository and a bug report, RepoPilot searches the codebase, inspects the relevant
code, proposes a patch, validates it with pytest inside an isolated Docker sandbox,
and iterates on failures under a fixed step / token / cost / runtime budget — while
recording a structured trace of everything it did.

The point is not the LLM call. The point is measurement: task success on a
deterministic benchmark, retrieval recall, cost, latency, and a failure taxonomy,
with every added feature justified by an ablation.

**Status: Phase 0 (evaluation harness) — done; Phase 1 (baseline agent) — done;
Phase 2a (structured runtime) — done and measured against the baseline, results
below.** Every number here is measured by a run archived under
`evals/experiments/`; nothing is a placeholder.

## Results so far

The baseline agent (a plain tool-calling loop, no runtime controls, no
retrieval beyond grep and an `ast` symbol table) on RepoPilot-Bench v0 (14
controlled-mutation tasks), every configuration under the spec §9.1 budget
(30 steps, 40 tool calls, 5 test runs, 100k tokens, $0.50, 600 s per task):

| model | runs | success | steps | tokens / task (median) | cost / task (median) | time (p50) | ended by budget | invalid tool calls |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `claude-sonnet-5` | 3 × 14 | **42 / 42** | 5.9 | 18.7k | $0.045 | 15 s | 0 | 1.4% |
| `gpt-5.6-luna` | 1 × 14 | **14 / 14** | 7.7 | 33.7k | $0.003 | 18 s | 0 | 8.3% |
| `claude-haiku-4-5` | 1 × 14 | **11 / 14** | 14.3 | 90.8k | $0.105 | 27 s | 6 / 14 | 0.0% |

Runs of 2026-09-14, archived under `evals/experiments/baseline-v0-*/`. Sonnet's
three runs (one single, one `--repeat 2`) gave identical verdicts on every task;
trajectories vary (7 / 10 / 8 steps on `cachetools_001`).

Two findings, both about what v0 can and cannot measure:

1. **Success rate on v0 is saturated for capable models.** The tasks are
   single-site mutations in small libraries, and even the "hard" bug reports
   carry the name of the class or decorator involved, so `search_symbol` on a
   name from the report lands on the right function in one call (Sonnet: 12 / 14
   runs opened that way, the two hidden-only tasks passed without the agent ever
   seeing a failing test). A benchmark the baseline completes cannot show what
   hybrid retrieval adds; that needs **Bench v1** (reports audited for leaked
   identifiers, larger repositories, symptoms in a different module than the
   cause, more hidden-only tasks).
2. **Efficiency under a fixed budget is not saturated, and the weaker model shows
   where a runtime would help.** Haiku spent 5× Sonnet's tokens per task (5.4
   file reads per task vs 1.5; it re-reads test files and keeps searching after it
   has the answer), hit the 100k-token cap in 6 of 14 runs, and all three of its
   failures were *analysis without an edit* — `tenacity_004`'s trace ends with the
   correct diagnosis written out and no `edit_file` call. Three of its passes
   also ended on the cap after the tests were already green. Luna's only errors
   were 12 calls to `edit_file` with an invented argument name (`replacement`),
   one per task, each repaired on the next step. Explicit termination on green
   tests, repeat detection and tool-argument validation (spec §5, Phase 2) have
   measurable targets on v0 today: budget terminations, wasted steps after
   success, invalid-call rate, tokens per task.

Details in [Phase 1 findings](#phase-1-findings).

### Structured runtime vs. baseline (Phase 2a, spec §12.2)

Same 14 tasks, same budget, same models; the runtime owns reproduction,
verification, termination, phase tool sets and loop detection (details in
[The structured runtime](#the-structured-runtime-phase-2a)):

| model | arm | success | steps | tokens / task (median) | cost / task (median) | time (p50) | ended by budget | invalid tool calls |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `claude-haiku-4-5` | baseline | 11 / 14 | 14.3 | 90.8k | $0.105 | 27 s | 6 / 14 | 0.0% |
| | **structured** | **12 / 14** | 12.3 | 60.9k | $0.067 | 25 s | 2 / 14 (+1 loop) | 0.0% |
| `gpt-5.6-luna` | baseline | 14 / 14 | 7.7 | 33.7k | $0.003 | 18 s | 0 | 8.3% |
| | **structured** | **14 / 14** | 8.1 | 39.0k | $0.004 | 20 s | 0 | 10.8% |
| `claude-sonnet-5` | baseline (3 × 14) | 42 / 42 | 5.9 | 18.7k | $0.045 | 15 s | 0 | 1.4% |
| | **structured** | **14 / 14** | 6.9 | 22.4k | $0.053 | 16 s | 0 | 0.0% |

Archived as `evals/experiments/structured-v0-*/`. The runtime moved what it was
built to move — on Haiku, budget terminations 6 → 2, tokens per task −33%,
cost −36%, and no run kept exploring after its tests were green — but success
rate stayed within one task of the baseline (one task is 7 points), and the
capable models paid for the structure: one extra step (PLAN) and +19% (Sonnet)
/ +55% (luna, on $0.003) cost, with luna's invented-argument rate unchanged as
predicted. The pre-registered expectation of 14 / 14 for Haiku was not met; the
traces say why in [Phase 2a findings](#phase-2a-findings), and they point at
the two next changes: tolerant exact-text edits and context compaction.

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
uv run python -m evals.runner --solver structured --model claude-haiku-4-5-20251001 --max-run-cost 10  # Phase 2a runtime
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

Across models (`evals/experiments/baseline-v0-haiku45/`, `baseline-v0-luna/`):

- **Haiku 4.5 (11 / 14).** 14.3 steps and 90.8k tokens per task against Sonnet's
  5.9 and 18.3k: 5.4 file reads per task, many of them test files, and searches
  repeated after the answer was in context (`tenacity_003`: 11 search calls, no
  edit). Six runs ended on the 100k-token cap. Three of those had already produced
  a correct patch and kept exploring (`toolz_003`: tests green at step 13, eight
  more steps of reading); the three failures never called `edit_file` at all, and
  in `tenacity_004` the final message states the exact fix. Cost per task ($0.105)
  ended up *higher* than Sonnet's despite the lower price per token, because the
  loop resends the whole history every step — cumulative tokens grow with the
  square of the trajectory length.
- **gpt-5.6-luna (14 / 14, $0.003 per task).** Fewer steps than Sonnet but more
  tool calls (1.3 per step; it issues parallel calls) and one invented argument
  per task: `edit_file(..., replacement=...)` instead of `new_string`, rejected by
  the validator and corrected on the next step — 12 of its 144 calls, the 8.3%
  invalid rate. The schemas are sent without OpenAI's strict mode, so this is
  exactly the kind of error runtime tool validation should absorb.
- **Localization never failed — and that is the leak.** The first `edit_file`
  landed in a gold file in 71 / 71 runs that edited, across all three models,
  through two channels: Sonnet takes the identifier from the report
  (`search_symbol` first in 38 / 43 runs, the query verbatim from the report in
  41 / 43); Haiku and luna run the tests first (16 / 17 and 8 / 14) and follow
  the failing test to the function. A scan of all 74 traces
  (`scripts/leak_scan.py`) found no hidden test file or hidden test name in
  anything the agent saw, and neither the sandbox nor the workspace exposes a
  history to diff against; what gives the bugs away is the repository itself —
  comments, docstrings and release notes that describe the reverted behaviour
  (`tenacity_004`), and textbook fault patterns (`cache={}`, `if not cache`,
  `type(e) in types`). `docs/leak-audit.md` has the audit and a four-arm
  ablation (report / tests / neither) to measure each channel; with a larger
  budget (300k tokens) Haiku also solved its three failures (15–24 steps,
  $0.13–0.21 each), so they were budget failures, not capability failures.
- **Statistical caveat.** With 14 tasks one task is 7 points; Haiku's 78.6% has a
  95% interval of roughly 52–93%. Verdict-level comparisons on v0 need repeats,
  and success-rate claims need the larger v1. Token, step and termination
  metrics are far less noisy and already separate the three models cleanly.

Consequences, in order: (1) **Phase 2 (structured runtime)** can be evaluated on
v0 now — the treatment arm is measured against this baseline on the same three
models, with success-under-budget for Haiku, budget terminations, steps after the
tests go green, invalid-call rate and tokens per task as the outcomes; (2)
**Bench v1** (reports without leaked identifiers, repositories an order of
magnitude larger, cross-module symptoms, more hidden-only tasks, 30+ tasks) is
required before the retrieval work of Phase 3 can show anything on success rate.

## The structured runtime (Phase 2a)

`--solver structured` runs the same tools, workspace, sandbox, budget and model
through a state machine that owns control (spec §5; design and pre-registered
expectations in `docs/runtime-design.md`):

```
INITIALIZE  runtime  run the task's tests once, before any change (initial_failures)
PLAN        model    no tools; {"plan": [...], "suspects": [...]}
LOCALIZE    model    search_code / search_symbol / find_references / read_file;
                     a reply without tool calls is the hypothesis
PATCH       model    the same plus edit_file, never run_tests; a reply without tool
                     calls and a changed diff hands over to TEST
TEST        runtime  the full test command; fixed / still failing / newly failing
                     green -> DONE (FINALIZE first when the suite started green)
ANALYZE     model    no tools; {"diagnosis", "next": "patch"|"localize", "keep_patch"}
```

What the runtime does that the baseline leaves to the model: it reproduces
before planning, verifies after every patching turn, and ends the run on a green
full-suite run (no exploring after success); it refuses tools outside the phase
and asks for the hypothesis when a phase's tool budget (10 calls) is spent; it
refuses the third identical tool call with a replanning notice and ends the run
as `agent_loop` on the fourth (spec §9.2); it nudges once when a patching turn
ends without an edit and ends the run as `no_progress` on the second; it reverts
the workspace when ANALYZE says so. `max_test_runs = 5` now means one
reproduction plus at most four patch → test rounds. The conversation history is
kept in full — 2a changes control only, so the architecture ablation (spec
§12.2) isolates it; context compaction is Phase 2b.

Every run records the phase transitions, the plan, the hypotheses and their
fate, each test run's fixed / still-failing / newly-failing sets and the
intervention counters (`agent.runtime` in `results.jsonl`, `phase` / `decision`
/ `test_run` / `intervention` / `state` events in the trace); the summary adds
`verified_rate`, `loop_rate`, steps by phase and the intervention totals.
### Phase 2a findings

Runs of 2026-09-14 (`evals/experiments/structured-v0-haiku45/`, `-luna/`,
`-sonnet5/`; the first Haiku run, made before the fix of issue #7, is kept as
`structured-v0-haiku45-run1`). Read against the expectations written down
beforehand in `docs/runtime-design.md`:

- **The controls did their job.** No structured run spent a step after its
  tests were green (the baseline's Haiku wasted 8 steps on `toolz_003` alone);
  Haiku's `budget_tokens` terminations fell from 6 to 2 in both structured
  runs, and one of those two still passed because its last edit was in place
  when the cap hit. Two runs needed the runtime to ask for the hypothesis after
  10 localization calls; two reverted a patch on request; one hit the loop
  detector.
- **Success did not move beyond noise.** Haiku: 11 / 14 → 12 / 14 in both
  structured runs, but with different failures each time (`tenacity_004` +
  `toolz_001`, then `cachetools_003` + `toolz_001`), while the baseline's three
  failures (`cachetools_005`, `tenacity_003`, `tenacity_004`) all passed under
  the runtime. With 14 tasks and a model this variable, the success metric
  needs repeats before it says anything; the efficiency metrics already do.
- **What still fails, from the traces.** (1) *Exact-text edits*: `toolz_001`
  failed both times the same way — 7 `edit_file` calls alternating between a
  4- and a 5-space indentation copied from the numbered listing, none matching
  the file's 15-space continuation line; the second run ended as `agent_loop`
  when the identical call came back a fourth time. Across the 130 traces of
  both arms, 14 `edit_file` calls failed on whitespace — none in the baseline
  runs, 12 in the structured Haiku runs (10 of them on `toolz_001`), 2 in
  luna's. (2) *Context
  growth*: a 15-step run whose reads replay 100–150 lines each still reaches
  100k tokens (`cachetools_003`: 103k at step 15, no patch left). (3) *A poor
  decision the runtime executed faithfully*: `cachetools_003`'s first patch
  fixed both initially failing tests and broke one; ANALYZE chose "revert and
  localize again", which threw the progress away and ran out the budget
  reading. The baseline's Haiku had passed this task by patching the guard.
- **One runtime bug, found by reading the first run.** `tenacity_004` ended
  `budget_tokens` on the very reply that carried the correct edit, because the
  cap was checked before the reply's tool calls ran (issue #7). Fixed: a reply's
  tool calls execute first; the run then ends as `budget_tokens` with its patch
  in the workspace, as the baseline always did. `tenacity_004` passed on the
  re-run.
- **Cost of structure on strong models.** Sonnet: 5.9 → 6.9 steps, $0.045 →
  $0.053 (+19%, within the +20% pre-registered), tool calls ≈5 → 3.9 (the
  runtime's reproduction run replaces the model's own test calls). luna: 7.7 →
  8.1 steps, tokens +16% but cost +55% ($0.003 → $0.0045; the JSON decision
  points cost it reasoning tokens), invalid calls 8.3% → 10.8% (14 of 129, the
  same `edit_file(replacement=…)` habit; nothing in 2a targets it).

Next, in order: **2a.1** (built; runs pending — pre-registered in
`docs/runtime-design.md` §7) — an `edit_file` that falls back to a unique
whitespace-tolerant match when the exact text is not found, re-indents the
replacement to the file and reports it, an error that shows the closest region
of the file verbatim, and an ANALYZE prompt that states the progress a patch
made before offering to revert it; both arms count `edits`, `failed_edits`
and `tolerant_edits`. **2b** — context compaction (spec §8.1 / §12.3),
measured on tokens per task with success held constant. Both are measured on
Haiku first, where the failure modes live.

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
                           (exact match, else a unique whitespace-tolerant match re-indented to the file)
  tools/toolbox.py         the six tool schemas the model sees + dispatch/validation + run_tests
                           (workspace diff -> sandbox -> per-test summary)
  agent/budget.py          AgentBudget (spec §9.1) + BudgetTracker
  agent/prompts.py         system / task prompts of the baseline; phase prompts of the runtime
  agent/run.py             Termination reasons + AgentRun (metrics, patch, trace) shared by both agents
  agent/baseline.py        BaselineAgent: the budgeted tool loop (Phase 1, the control arm)
  agent/state.py           AgentState (spec §5.2): plan, hypotheses, test history, counters; Phase
  agent/policies.py        phase tool sets, per-phase limits, LoopDetector (spec §9.2), JSON parsing
  agent/runtime.py         StructuredAgent: the state machine (Phase 2a, the treatment arm)
  tracing/events.py        Trace: JSONL events (model_call, tool_call, phase, test_run, decision,
                           intervention, state, patch, run_end)
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
  solvers.py               Solver protocol; `null` and `gold` oracles; `baseline` and `structured`
                           agent solvers (same workspace / sandbox / budget, different agent)
  harness.py               build image -> solve -> evaluate in a fresh sandbox -> TaskResult
                           (+ agent record, trace file, test-file edits stripped from patches)
  metrics.py               agent metrics (spec §11.1) and the failure taxonomy (spec §11.2)
  runner.py                CLI: results.jsonl, summary.json, logs/, traces/, --repeat, --expect,
                           --model and --max-* budget flags, --max-run-cost
docker/base.Dockerfile     base image for sandbox containers
docs/benchmark-authoring.md  how tasks are made: target repos, workflow, rules, coverage plan
docs/issues.md             engineering log: symptom -> root cause -> fix -> guard
docs/leak-audit.md         why v0 saturates: what the agent cannot see, what it did see, ablation plan
docs/runtime-design.md     Phase 2a design and the pre-registered expectations for the runtime runs
evals/experiments/         archived runs behind the numbers in this README (summary, results, traces)
scripts/                   make_task.py, validate_tasks.py, export_task_schema.py, model_smoke.py,
                           archive_run.py (results/<run> -> evals/experiments/<name>),
                           leak_scan.py (traces never contain a hidden test; how each model localizes)
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
