# RepoPilot on Bench v0 — Phases 1–3 (2026-09-10 → 2026-09-19)

The v0 chapter of the project, moved here from the README on 2026-09-28 when
Bench v1 became the headline; the text is unchanged apart from this note and
one link. Each phase was measured on v0 first, with its expectations written
down beforehand (`docs/runtime-design.md`, `docs/retrieval-design.md`,
`docs/leak-audit.md`), and the v0 saturation these runs revealed is what
Bench v1 was built to remove. Every number is from a run archived under
`evals/experiments/`.

Contents: the [v0 results](#results-on-repopilot-bench-v0-phases-13) (baseline
and structured runtime, all three models), the per-phase findings
([Phase 1](#phase-1-findings), [Phase 2a / 2a.1 / 2b](#phase-2a-findings),
[Phase 3 / 3.1](#retrieval-on-v0-phase-3-and-31)), [the v0 tasks](#repopilot-bench-v0)
and the [Phase 0 checklist](#phase-0-checklist).

## Results on RepoPilot-Bench v0 (Phases 1–3)

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

### Structured runtime vs. baseline (Phase 2, spec §12.2 and §12.3)

Same 14 tasks, same budget, same models, same tools; the runtime owns
reproduction, verification, termination, phase tool sets and loop detection
(details in [The structured runtime](../README.md#the-structured-runtime)), and
its `compact` context rebuilds every prompt from the runtime's working state
plus the last three tool steps instead of replaying the whole history
([2b](#phase-2a-findings)):

| model | arm | success | steps | tool calls | test runs | tokens / task (median) | cost / task (median) | time (p50) | ended by budget | verified on a green run | invalid calls | repeatable verdicts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `claude-haiku-4-5` | baseline (2 × 14) | 26 / 28 | 14.4 | 13.9 | 3.2 | 101.6k | $0.111 | 27 s | 13 / 28 | – | 0.0% | no (2 tasks flip) |
| | **structured, full history** (3 × 14) | **40 / 42** | 11.2 | 8.4 | 2.1 | 57.6k | $0.066 | 22 s | 4 / 42 | 90.5% | 0.3% | no (1 task flips) |
| | **structured, compact context** (3 × 14) | **40 / 42** | **10.9** | 8.5 | 2.2 | **38.4k** | **$0.047** | 24 s | **2 / 42** | **95.2%** | 1.7% | no (2 tasks flip) |
| `gpt-5.6-luna` (1 × 14) | baseline | 14 / 14 | 7.7 | 10.3 | 2.1 | 33.7k | $0.003 | 18 s | 0 | – | 8.3% | – |
| | structured, full history | 14 / 14 | 8.1 | 9.2 | 2.1 | 39.0k | $0.0045 (64% of input served from OpenAI's prompt cache) | 20 s | 0 | 100% | 10.9% | – |
| | structured, compact context | 14 / 14 | 8.3 | 10.4 | 2.1 | 32.5k | $0.0074 (0% cached) | 20 s | 0 | 100% | 8.9% | – |
| `claude-sonnet-5` (1 × 14; baseline 3 × 14) | baseline | 42 / 42 | 5.9 | ≈5 | 1.1 | 18.7k | $0.045 | 15 s | 0 | – | 1.4% | yes |
| | structured, full history | 14 / 14 | 6.9 | 3.9 | 2.0 | 22.4k | $0.053 | 16 s | 0 | 100% | 0.0% | – |
| | structured, compact context | 14 / 14 | 6.9 | 3.9 | 2.0 | 23.2k | $0.054 | 23 s | 0 | 100% | 0.0% | – |

Haiku rows: `evals/experiments/baseline-2a1-haiku45-x2/`,
`structured-final-full-haiku45-x3/` and `structured-final-compact-haiku45-x3/`
— the two structured arms are one experiment on the final code, run back to
back (2026-09-18, pre-registered in `docs/runtime-design.md` §9); the
baseline is the earlier 2 × 14 run, its code unchanged since. Luna and Sonnet
rows: `baseline-v0-*`, `structured-v0-*` (full history, 2026-09-15, the tool
version before 2a.1) and `structured-2b-luna` / `structured-2b-sonnet5`
(compact, 2026-09-18).

What the table says. The runtime does not raise Haiku's success rate on v0 —
26 / 28 baseline, 40 / 42 in both structured arms, all within one task of
each other — but it changes how Haiku gets there: about 40% fewer tokens and
cost per task with the full history (−43% / −41%), a fifth of the budget
terminations, fewer steps, tool calls and test runs. Compaction then takes
another third off:
38.4k tokens and $0.047 per task, a 62% / 58% cut against the baseline, at the
same success, the same steps and more runs ending on a verified green suite
(95% vs. 90%), because the runs that used to hit the 100k cap now finish.
Its costs are visible too: 17 re-reads of code that had left the window in
42 runs, more interventions (10 forced hypotheses, 4 nudges against 6 and 0),
and 1.7% invalid calls — Haiku copying the state's "lines 418-450" notation
into `read_file(start="[418, 450]")`. No arm is deterministic at three
repeats: `cachetools_003` (a patch that fixes the two failing tests and breaks
a third; the model then re-reads `__touch` and the test until the cap) passes
1 of 3 full runs and 2 of 3 compact runs, and one compact run of
`cachetools_001` went after the wrong method first and ran out of budget
re-localising. The capable models pay for the structure: one extra step
(PLAN) and +19% (Sonnet) / +55% (luna, on $0.003) cost per task with the full
history.

Compaction on the capable models is the negative result of Phase 2, and it
has two different causes. Sonnet finishes in 6.9 steps, and a run that short
never grows past the window: its prompts are the same size under either
context (3.2k tokens per call, median), so tokens and cost are unchanged
(+3%, flat) — compaction only pays once the history is longer than the state
plus three steps, which on v0 is Haiku's problem, not Sonnet's. Luna's
prompts *did* shrink (4.9k → 2.5k per call, −17% tokens per task) and the task
still cost 64% more, because OpenAI caches prompt prefixes automatically and
bills cache hits at a tenth: with the full history 64% of luna's input tokens
were cache reads; a state that is rebuilt every step has no stable prefix,
so the compact arm cached nothing. (Anthropic caches only on request; no
Claude run here used it, so the Haiku and Sonnet comparisons are between two
uncached arms.) Tokens per task, the metric the design was aimed at, would
have called luna a win; cost per task says the opposite, and cost is the
metric that matters. Prompt caching on the full-history arm is therefore the
next lever to measure, not a footnote. The traces behind each number are in
[Phase 2a findings](#phase-2a-findings).


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

- **2a.1 — tolerant edits and a progress-aware ANALYZE** (pre-registered in
  `docs/runtime-design.md` §7, measured with `--repeat 2` on both arms). `edit_file`
  now falls back to a unique whitespace-tolerant match when the exact text is
  not found, re-indents the replacement to the file and says so; a miss shows the
  closest region of the file verbatim; ANALYZE states what the patch fixed and
  broke before offering to revert it. Results against the expectations:
  whitespace-failed edits 12 → **0** (5 structured and 3 baseline edits went
  through the fallback, every one of them in a run that passed); `toolz_001`
  passed both times (7 and 10 steps instead of 18–19); `agent_loop` 0; no patch
  that had fixed initial failures was reverted (the two resets were on
  `toolz_003` patches that fixed nothing and broke one test — the right call);
  structured success 13 / 14 in both runs, with identical verdicts. The one
  expectation that failed is the control arm: the baseline moved from 11 / 14 to
  12 / 14 and 14 / 14, more than "within one task". Its three tolerant edits were
  on tasks it had already passed, so the lift is run-to-run variance — its two
  former failures passed on `budget_tokens` with the patch in place — not the
  tool. Conclusion: with the same tools, the arms tie on success (26 / 28 each),
  and the runtime wins on every efficiency and reliability metric.
- **What remains: one task, near the cap.** `cachetools_003` fails under the
  runtime the same way each time: the first patch fixes both initially failing
  tests and breaks `test_missing_getsizeof`; ANALYZE keeps the patch (the 2a.1
  framing worked) but goes back to localizing, re-reads 100–150-line slices,
  and the run hits the 100k-token cap at step 15–16 with the regression still
  in. Deterministic over the first two runs, it passed 1 of 3 in the final
  full-history experiment and 2 of 3 with the compact context: the guard
  (`key in self.__order`) is found when the model reaches it before the cap.
  The baseline fails the same task in one of two runs. That the cap is where
  it fails is what Phase 2b was for.

- **2b — context compaction** (`--context compact`, built; pre-registered in
  `docs/runtime-design.md` §8). Every model call is rebuilt from the runtime's
  working state (task, initial failures one line each, plan, hypotheses,
  diagnoses, files read as line ranges, searches, the current diff, the latest
  test run, budget left) plus the last three tool-calling steps verbatim and
  the current phase's instructions; nothing is summarised by a model. A replay
  of the 2a.1 traces predicted 55–60% of the cumulative tokens. Two Haiku
  runs so far (2 × 14 each, `evals/experiments/structured-2b-haiku45-x2-run1`
  and `-run2`), each followed by a runtime fix the traces demanded. Run 1:
  −31% tokens, −24% cost, 25 / 28 — both `toolz_003` failures were the loop
  detector refusing re-reads its own context strategy had made necessary
  (issue #8: signatures now carry the workspace version, only calls the model
  can still see count, re-reads are tallied as `rereads`). Run 2: −38%
  tokens (median 34.8k), −32% cost ($0.043), 27 / 28 at the same 10.8 steps —
  but `toolz_003` still ended `agent_loop` twice, now with the right patch in
  place: the compact arm re-sent the one-time note "your change has been
  reverted" on every turn after the model's next edit (issue #9: runtime
  events are now dated in the state, the re-sent instructions are bare, and
  the state says which patch the latest test run saw). Run 3, on the code as
  it stands (`evals/experiments/structured-2b-haiku45-x2`): 26 / 28
  with the full arm's exact verdict pattern (`cachetools_003` fails both
  repeats, everything else passes both), `agent_loop` 0, tokens median 38.1k
  (−32%), cost $0.046 (−27%), steps 11.2 vs. 10.9, nine re-reads of code that
  had left the window. The compact arm was frozen there, and the final
  experiment (§9: both structured arms on this code, `--repeat 3`, back to
  back) is the table at the top: 40 / 42 in both arms, 57.6k → 38.4k tokens
  and $0.066 → $0.047 per task, verified green runs 90.5% → 95.2%, budget
  terminations 4 → 2, steps 11.2 → 10.9; the price is 17 re-reads in 42 runs,
  more forced hypotheses and nudges (10 / 4 against 6 / 0), and 1.7% invalid
  calls where the state's `418-450` range notation came back as
  `start="[418, 450]"`. Every §9 expectation held except the failing tasks'
  names: `cachetools_003` passed twice in three compact runs, and
  `cachetools_001` failed once (a wrong first method, then re-localisation to
  the cap). On Sonnet and luna, compaction did not pay (Sonnet's runs are
  too short to compact; luna's cost rose 64% because rebuilding the prompt
  forfeits OpenAI's automatic prefix cache — see the table). A
  threshold-triggered hybrid and model-written summaries stay out of scope
  (§8.3); prompt caching on the full-history arm is the open measurement.


### Retrieval on v0 (Phase 3 and 3.1)

What v0 can measure about it is written down first, in
`docs/retrieval-design.md`: the leak audit showed localization is free on v0,
so the prediction is that success does not move; what should move is the
cost of localization (LOCALIZE was 253 of Haiku's 456 compact steps). Retrieval
*quality* is measured offline — the report as the query, the task's gold
files and symbols as the relevant set (`scripts/retrieval_eval.py`, no model):

| configuration | Recall@5 file | Recall@10 file | MRR file | Recall@10 symbol | MRR symbol | query (p50) |
| --- | --- | --- | --- | --- | --- | --- |
| BM25 | 0.93 | 0.93 | 0.86 | 0.93 | 0.54 | 1.7 ms |
| dense (bge-small) | 0.93 | **1.00** | 0.87 | **1.00** | 0.81 | 36 ms |
| symbol | 0.86 | 0.93 | 0.65 | 0.93 | 0.48 | 0.5 ms |
| BM25 + dense | 0.93 | 0.93 | 0.86 | 0.93 | 0.80 | 39 ms |
| BM25 + dense + symbol | 0.93 | 0.93 | **0.93** | 0.93 | **0.89** | 43 ms |

`evals/experiments/retrieval-v0/` (2026-09-18, 14 tasks, 6,467 chunks; the
first index of each repository takes 56–100 s to embed on a laptop CPU, every
later task of the same repository 0.2–0.4 s from the cache). Two things the
pre-registration got wrong, in opposite directions: the small embedding model
is the best single channel, not the weakest (predicted 0.60–0.80 recall,
measured 1.00), and fusion does not dominate its inputs. On `cachetools_005` —
the one v0 task whose report names only the public surface (`@cached(...)`)
while the bug sits in the private module it delegates to — only the dense
channel reaches the gold (ranks 6 and 10), and reciprocal rank fusion, which
rewards agreement between channels, lets the lexical and symbol channels'
shared wrong picture outvote it: fused Recall@10 stays at BM25's 0.93 while
fused MRR is the best of any configuration (the gold is rank 1 for 13 of 14
tasks). That task is the shape Bench v1 will have more of, and where the
retrieval configurations will separate.

Agent level (Haiku, compact context, 2 × 14 per arm, `evals/experiments/structured-p3-{tool,evidence}-haiku45-x2`,
against the Phase 2 compact control):

| arm | success | steps | LOCALIZE steps / run | PATCH steps / run | tool calls | tokens / task (median) | cost / task | `retrieve` calls | forced hypotheses / run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `none` (Phase 2 final, 3 × 14) | 40 / 42 | 10.9 | 6.0 | 3.5 | 8.5 | 38.4k | $0.047 | – | 0.24 |
| `tool` | 27 / 28 | 11.3 | 7.0 | 3.0 | 8.6 | 41.3k | $0.050 | **0** | 0.29 |
| `evidence` | 27 / 28 | 10.5 | **5.2** | 4.0 | **7.1** | 41.2k | $0.050 | **0** | **0.14** |

Two results, neither the one pre-registered. Haiku never called `retrieve`
— not once in 56 runs with the tool in every LOCALIZE and PATCH prompt — so
a retrieval tool the model *may* use is no retrieval for this model, and the
`tool` arm is a re-run of the control (same 27 / 28, `cachetools_003`). The
evidence the runtime pushed at PLAN (the gold file at rank 1 in 26 of 28
runs) did shorten localization — LOCALIZE steps −26%, `search_symbol` calls
39 → 6, forced hypotheses halved, several runs hypothesising at their first
turn with no tool call — and the runtime gave the saving back: the compact
state dropped the evidence at PATCH, `edit_file` needs the exact text, and 28
of the arm's 33 PATCH-phase reads re-read what the evidence had shown
(issue #12). Tokens per task ended flat. The same runs exposed a bug in the
2a.1 whitespace-tolerant edit (inserted lines re-indented by the wrong
offset, and a model correcting the indentation overruled by the file — issue
#11; one `toolz_003` run spent 12 steps and hit the cap with the right patch
already in place). Both were fixed and the experiment re-run with the
expectations written first (`docs/retrieval-design.md` §9; the control
re-run too, because a shared tool changed).

**Phase 3.1** (Haiku, compact, 2 × 14 per arm,
`evals/experiments/structured-p31-{none,evidence}-haiku45-x2`; §10 of the
design doc has the full reading):

| arm | success | steps | LOCALIZE / PATCH steps per run | tool calls | re-reads after the window | LOCALIZE cap reached | tokens / task (median · mean) | cost / task | `budget_tokens` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `none` | **28 / 28** | 11.4 | 6.6 / 3.6 | 9.0 | 18 | 10 | 37.3k · 44.4k | $0.047 | 0 |
| `evidence` | 27 / 28 | **8.9** | **4.8 / 2.9** | **5.6** | **6** | **4** | 36.8k · 42.3k | $0.045 | 1 |

Every step-side expectation held with margin — steps −22%, tool calls −38%,
`search_symbol` calls 41 → 5, PATCH back under 3 steps (the re-reads: 2 of
11 PATCH reads while the evidence is in view, down from 28 of 33), the
evidence holding the region the model went on to edit in 26 of 28 runs (the
two misses are `cachetools_005`, the offline miss; both passed by search) —
and the token expectation did not: median −0.5k against a pre-registered
≥ 2k, mean −4.8%, cost −6%. The reason is in the per-phase numbers: each
call before the first edit carries the evidence block, so PLAN 2.2k → 3.5k
input tokens, LOCALIZE 3.7k → 4.7k per call, PATCH 4.0k → 4.6k; 22% fewer
calls at 20–26% more tokens each is a wash at the median, a saving only on
the runs that were long without it (`cachetools_001` 70k → 52k) and a cost
on the short ones (`tenacity_003` 24k → 31k, seven steps either way). So
the pre-registered fallback is the conclusion: on v0, where localization is
free, runtime-injected evidence is a step, tool-call and re-read saver, not
a token saver. The one failure, `cachetools_003.1`, is the benchmark's
hardest task on a budget edge, not an evidence effect — 17–22 steps and
70–101k tokens in all four runs across both arms, the first patch breaking
`test_missing_getsizeof` every time; this run's second patch used a
name-mangled attribute that does not exist in the subclass and the run hit
the 100k token cap at 100,984, one TEST run short of seeing it. The two
whitespace-matched edits per arm all landed at the right indentation (issue
#11's shape among them); the edit-cap forced test never fired in 56 runs.


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

Phase 0 is complete; Phases 1–3 and Bench v1 followed, in that order, each
measured against the previous one — the v1 results are in the README, the
run sheets under `docs/`, the runs under `evals/experiments/`.
