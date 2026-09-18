# Phase 2a — the structured runtime

Written 2026-09-15, before the first run. This is the design *and* the
pre-registered experiment: the "expected" column below was filled in before any
treatment run existed, so the results can be read against it honestly.

## 1. What failure mode, what metric (spec §19)

Phase 1 measured the baseline (an unconstrained tool loop) on Bench v0 with three
models. Success rate is saturated for the capable models, so the runtime is
judged on the failure modes the traces actually show:

| failure mode (Phase 1 evidence) | runtime response | metric that must move |
|---|---|---|
| Exploring after the tests are green (Haiku: 3 of 14 runs, e.g. `toolz_003` green at step 13, eight more steps) | the runtime, not the model, decides DONE: a green full-suite run with a non-empty diff ends the run | steps after first green → 0; `budget_tokens` terminations 6 → 0 |
| Analysis without action (Haiku: 3 failures never called `edit_file`; `tenacity_004` states the fix in its last message) | HYPOTHESIZE → PATCH is a forced transition; a turn that ends without a new edit gets one nudge, then `no_progress` | Haiku success under the §9.1 budget 11 / 14 → 14 / 14 |
| Repeated searches (`tenacity_003`: 11 search calls, no edit) | loop detection (§9.2): the third identical call is refused with a replanning message, a fourth ends the run as `agent_loop`; per-phase tool-call limits | loop interventions (expected > 0 on Haiku), tool calls per task |
| Invented arguments (luna: 12 / 144 calls `edit_file(replacement=…)`) | **not addressed in 2a**: validation and its error message (which already names the expected parameters) are unchanged; OpenAI strict-mode schemas are a separate, later change | invalid rate expected unchanged (~8%) — reported, not claimed |
| Quadratic context growth (Haiku 5.5k tokens per step, 91k per task) | **not addressed in 2a** — Phase 2b measures context compaction separately (spec §12.3) | tokens per task (2b) |

Sonnet and luna are already at 14 / 14; for them the criterion is "no worse":
success unchanged, steps and cost up by at most the PLAN call.

## 2. State machine (spec §5.3, adapted)

```
INITIALIZE   runtime   create the workspace; run the task's test command once
                       -> initial_failures (names + assertion messages)
PLAN         model     no tools, one call; JSON {plan: [...], suspects: [...]}
LOCALIZE     model     tools: search_code, search_symbol, find_references, read_file
                       exit: a reply without tool calls = the hypothesis
                       (or the phase's tool-call limit -> the runtime asks for it)
PATCH        model     tools: the four above + edit_file (never run_tests)
                       exit: a reply without tool calls and a diff that changed
                       since the last test run -> TEST
TEST         runtime   full task command; fixed / still_failing / newly_failing
                       green and diff non-empty and initial failures existed -> DONE
                       green but the suite was already green at the start -> FINALIZE
                       otherwise -> ANALYZE
ANALYZE      model     no tools; JSON {diagnosis, next: "patch" | "localize", keep_patch}
                       keep_patch=false -> the runtime resets the workspace
FINALIZE     model     no tools; JSON {decision: "done" | "continue"} (continue once)
DONE
```

Differences from the spec's diagram, and why:

- **Reproduction is the runtime's job**, at INITIALIZE. It costs no model step,
  gives every model the "follow the failing test" channel that Haiku and luna
  used on their own, and gives the runtime the reference set for judging later
  runs (what was fixed, what still fails, what the patch broke).
- **SEARCH and INSPECT are one phase (LOCALIZE).** In the traces they alternate
  call by call; separating them would only add turns.
- **The model never runs tests.** TEST happens when a PATCH turn ends with a
  changed diff. Verification no longer depends on the model remembering to do
  it, and `max_test_runs = 5` means one reproduction plus at most four
  patch → test rounds. The budget is the same number with a documented
  meaning (spec §9.1).
- **Hidden-only tasks** (the visible suite is green from the start): the model is
  told so at PLAN, and a green run after the first patch goes to FINALIZE, where
  the model may continue once. Without that, any edit — right or wrong — would
  end the run.

## 3. Runtime responsibilities (spec §5.4)

- *Typed validation.* Unchanged from Phase 1 (`Toolbox.call`): unknown tool,
  unknown or missing argument, wrong type → an error result the model can act
  on, counted as an invalid call.
- *Phase tool sets.* A call to a tool outside the phase is refused with a
  message naming the phase and the way forward; counted as `refused_tool_calls`,
  not as invalid (the schema was fine; the policy was not).
- *Loop detection.* Signature = tool name + canonical JSON of the arguments +
  the workspace version (an edit or a reset makes the same call new — since
  issue #8). Third occurrence the model can still see → refused with a
  replanning message (`loop_interventions`); repeated while that message is in
  view → `agent_loop`. With the full history everything is in view; the compact
  context counts only its window (§8.4), and re-reads of what left it are
  tallied as `rereads`.
- *Phase limits.* LOCALIZE: 10 tool calls per visit, then the runtime asks for
  the hypothesis with no tools offered (`forced_transitions`). PATCH: 4 edits per
  visit, then edits are refused until the tests run.
- *Recoverable vs terminal.* Recoverable, each with one nudge: an empty reply,
  a PATCH turn without a new edit, an unparsable JSON decision (defaults are
  used). Terminal: any budget, `agent_loop`, `no_progress` (a second turn
  without progress), `model_error`, and `budget_test_runs` (a changed patch that
  can no longer be verified — the diff is still submitted).
- *Decision points.* PLAN, ANALYZE, FINALIZE and a forced hypothesis are model
  calls with the tool definitions still sent but `tool_choice="none"`: both
  providers reject a history containing tool calls unless tools are defined, and
  "none" keeps them defined but unusable, so the model can only answer in text.
- *State.* `AgentState` (plan, hypotheses with status, files visited, patches
  tested, test history, counters, phase) is carried by the `phase` trace event
  at every transition and by a final `state` event — auditable, and the seed of
  Phase 2b's compacted context.
- *Context.* Full conversation history, as in the baseline. 2a changes control
  only, so the §12.2 comparison isolates it.

## 4. Experiment (spec §12.2)

Same 14 tasks, same §9.1 budget, same three models; `--solver structured` against
the archived baseline runs. Expected, written before running:

| model | baseline | expected with the runtime |
|---|---|---|
| claude-haiku-4-5 | 11 / 14, 6 `budget_tokens`, 14.3 steps, 90.8k tokens | 14 / 14 under the same budget; 0 budget terminations; fewer steps; loop interventions > 0 |
| gpt-5.6-luna | 14 / 14, 8.3% invalid calls, 7.7 steps | 14 / 14; invalid rate about the same (nothing in 2a targets it); steps ≈ 7–9 (PLAN adds one, TEST removes the model's own run_tests turns) |
| claude-sonnet-5 | 42 / 42, 5.9 steps, $0.045 | 14 / 14; steps +1 to +2 (PLAN, and TEST no longer a model call); cost within +20% |

New per-run fields (`agent.runtime` in results.jsonl): `transitions` (with
step numbers), `steps_by_phase`, `plan`, `suspects`, `hypotheses`,
`initial_failures`, `test_history` (fixed / still failing / newly failing per
run), `loop_interventions`, `repeated_tool_calls`, `refused_tool_calls`,
`forced_transitions`, `nudges`, `analyze_rounds`, `workspace_resets`,
`finalize_continues`, `first_green_step`, `steps_after_green`, `verified`. New run-level metrics: `loop_rate` (runs with
at least one loop intervention), `verified_rate` (runs that ended on a green
full-suite run), plus the sums of the counters above. Failure taxonomy gains
`agent_loop` from the termination reason.

A negative result is a result: if Haiku still fails the same three tasks, the
runtime did not address the real failure mode and the traces say what did.

## 5. Not in 2a

Context compaction (§8.1 / §12.3), model routing (§12.4), retrieval (§7), any
change to the tool surface. The baseline agent is untouched; it is the control
arm.

## 6. Results (2026-09-14) against the expectations above

Runs: `evals/experiments/structured-v0-haiku45` (after the issue #7 fix; the
run before it is `structured-v0-haiku45-run1`), `structured-v0-luna`,
`structured-v0-sonnet5`; baselines `baseline-v0-*`.

| model | expected | measured | verdict |
|---|---|---|---|
| claude-haiku-4-5 | 14 / 14; 0 budget terminations; fewer steps; loop interventions > 0 | 12 / 14 (both runs); `budget_tokens` 6 → 2 (one of them a PASS); steps 14.3 → 12.3; tokens 90.8k → 60.9k; cost $0.105 → $0.067; loop interventions 1 (`toolz_001`); steps after green 0 | efficiency as expected, success **not** met |
| gpt-5.6-luna | 14 / 14; invalid rate ≈ unchanged; steps 7–9 | 14 / 14; invalid 8.3% → 10.8%; steps 7.7 → 8.1; cost +55% on $0.003 | as expected (cost not pre-registered) |
| claude-sonnet-5 | 14 / 14; steps +1 to +2; cost within +20% | 14 / 14; steps 5.9 → 6.9; cost +19%; tool calls ≈5 → 3.9 | as expected |

What the two Haiku failures per run were (trace paths under the archives):

- `toolz_001`, both runs: localization right by step 5–6, then 5–7 `edit_file`
  calls whose `old_string` never matched — the continuation line of the target
  statement is indented 15 spaces in the file, the model alternated between
  4- and 5-space renderings copied from the numbered `read_file` listing. Run
  1 eventually matched a shorter string and changed the wrong sub-expression;
  run 2 repeated an identical call a fourth time and the loop detector ended
  it (`agent_loop`). A tool-interface problem (spec §6), not a control problem.
- `tenacity_004`, run 1: the reply that crossed the token cap carried the
  correct edit and the runtime discarded it (issue #7). Passed on the re-run.
- `cachetools_003`, run 2: the first patch fixed both initially failing tests
  and broke `test_missing_getsizeof`; ANALYZE answered `keep_patch: false,
  next: localize`, the runtime reverted the workspace as asked, and the
  re-localization (reads of 100–150 lines, replayed every step) ran the token
  budget out at step 15 with no patch. The baseline's Haiku had passed this
  task by patching the guard on its first patch.

Reading: the runtime removed the failure modes it targeted (exploration after
green, analysis without an edit, budget terminations without a patch) and the
efficiency numbers moved by a third; the remaining Haiku failures are exact-text
editing and context growth, plus one revert decision the runtime made too easy
to take. Success on 14 tasks with this much run-to-run variance (the failing
tasks differ between the two structured runs) cannot separate 11 / 14 from
12 / 14; a claim about success needs `--repeat` on both arms.

Next: **2a.1** — `edit_file` falls back to a whitespace-tolerant match (exactly
one candidate after stripping leading whitespace per line) and its error shows
the closest region of the file; the ANALYZE prompt states what the patch fixed
and broke before offering the revert. **2b** — context compaction (§8.1 /
§12.3): the structured state instead of the full history, measured on tokens
per task at constant success. Both measured on Haiku first, with repeats.

## 7. 2a.1 — tolerant edits and progress-aware ANALYZE (written 2026-09-15, before running)

Two small changes, one tool-side and one prompt-side, each aimed at a failure
the traces showed twice:

- `edit_file` (shared by both arms): when the exact `old_string` is not found,
  the lines are matched ignoring leading and trailing whitespace; the match must
  be unique; the replacement is re-indented to the file (lines the model kept
  take their file indentation, a line indented like the old line in the same
  position takes that position's indentation, other lines follow the first
  line's offset). The result says when the fallback was used
  (`meta.match = "whitespace"`), and both agents count `edits`,
  `failed_edits` and `tolerant_edits`, aggregated in `summary.json` as
  `edit_failure_rate` and `tolerant_edits`. A miss now shows the closest
  region of the file verbatim, without line-number prefixes, so the next
  attempt can copy it.
- ANALYZE states the patch's progress first ("fixed N of M, broke K; reverting
  discards that") and reserves `keep_patch: false` for a change that is wrong
  in principle.

Because the tool is shared, the control arm changes too; the baseline is
re-run so both arms are measured with the same tool.

Runs: `--solver structured --model claude-haiku-4-5-20251001 --repeat 2` and
`--solver baseline --model claude-haiku-4-5-20251001 --repeat 2` (~$4.5).
Expected:

| metric | before (structured, 2 runs) | expected after |
|---|---|---|
| whitespace-failed `edit_file` calls (Haiku) | 12 over 2 runs, 10 on `toolz_001` | 0 failed on whitespace; `tolerant_edits` > 0 |
| `toolz_001` | failed twice (wrong sub-expression; `agent_loop`) | passes in both runs |
| `agent_loop` terminations | 1 | 0 |
| reverts of a patch that had fixed initial failures | 1 (`cachetools_003`) | 0 |
| structured Haiku success | 12 / 14, 12 / 14 | ≥ 12 / 14 per run; 13–14 / 14 if the two fixed modes were the binding ones |
| baseline Haiku success | 11 / 14 (1 run; 0 whitespace failures) | unchanged within one task — the tool change must not lift the control arm |
| tokens per task | 59–61k median | roughly unchanged (fewer wasted PATCH steps on the affected tasks only) |

The context-growth failures (`cachetools_003`-style: 15 steps of 100–150-line
reads) are not addressed here and are expected to remain; they are 2b's.

### 7.1 Results (2026-09-14, `structured-2a1-haiku45-x2` and `baseline-2a1-haiku45-x2`)

| metric | expected | measured | verdict |
|---|---|---|---|
| whitespace-failed `edit_file` calls (Haiku) | 0; `tolerant_edits` > 0 | 0 failed; tolerant 5 (structured) + 3 (baseline), all in runs that passed | ✓ |
| `toolz_001` | passes in both runs | passed twice: 10 and 7 steps (was 18–19), one tolerant edit each | ✓ |
| `agent_loop` terminations | 0 | 0 | ✓ |
| reverts of a patch that had fixed initial failures | 0 | 0 — the 2 resets were `toolz_003` patches that fixed nothing and broke one test | ✓ |
| structured Haiku success | ≥ 12 / 14 per run | 13 / 14 and 13 / 14, identical verdicts | ✓ |
| baseline Haiku success | unchanged within one task (11 / 14 ± 1) | 12 / 14 and 14 / 14 | ✗ — see below |
| tokens per task | roughly unchanged | 56.2k median (was 59–61k) | ✓ |

The control arm moved more than pre-registered. Its three tolerant edits were on
tasks it had already passed before 2a.1 (`cachetools_001`, `cachetools_002`,
`toolz_001`), and its former failures (`cachetools_005`, `tenacity_003`,
`tenacity_004`) passed this time on `budget_tokens` with the patch in place —
the same tasks it had lost on the cap before. The baseline's outcome on these
tasks is decided by *where* the cap falls relative to its last edit, which is
run-to-run variance, not the tool. Its verdicts differ between the two
repeats on two tasks; the runtime's do not.

Same tools, same budget, Haiku, 2 × 14 each:

| | baseline | structured |
|---|---|---|
| success | 26 / 28 | 26 / 28 |
| identical verdicts across repeats | no (2 tasks flip) | yes |
| steps / tool calls / test runs | 14.4 / 13.9 / 3.2 | 10.9 / 8.6 / 2.0 |
| tokens per task (median) | 101.6k | 56.2k |
| cost per task (median) / total | $0.111 / $2.64 | $0.063 / $1.82 |
| solve p50 | 27 s | 23 s |
| `budget_tokens` terminations | 13 / 28 | 3 / 28 |
| verified on a green full-suite run | – | 25 / 28 |

The remaining structured failure is deterministic: `cachetools_003` in both
runs — first patch fixes 2 / 2 and breaks `test_missing_getsizeof`, ANALYZE
keeps the patch (2a.1 worked) but goes back to localizing, re-reads 100–150-line
slices, and the token cap arrives at step 15–16 with the regression still in.
Context replay is the binding constraint; that is 2b's target, pre-registered
there.

## 8. 2b — context compaction (written 2026-09-15, before running)

### 8.1 Where the tokens go

In the 2a.1 structured Haiku runs (305 model calls), the first prompt of a run
is ~2k tokens (system prompt, task, reproduction output, PLAN instructions) and
the history then grows by a median of 395 tokens per step (mean 581, p90 1.5k,
max 2.4k). A 15-step run reaches 10–11k input tokens per call and ~100k
cumulative, of which the fixed prefix alone is ~30k. Simulating "fixed prefix +
the last K tool steps verbatim" on those 305 calls gives 69% (K = 2), 77%
(K = 3), 84% (K = 4) of today's cumulative input; shrinking the prefix as well
— the reproduction output (up to 6,000 characters, re-sent every step) becomes
one line per failing test — brings K = 3 to an estimated 55–60%, and the long
runs from ~100k to 40–45k. Those two numbers are the design's expectation.

### 8.2 Design (`--context compact`, structured solver only)

Every model call is rebuilt from the runtime's records; nothing is summarised by
a model, so the arm costs no extra calls and is deterministic:

```
system      RUNTIME_SYSTEM_PROMPT + how to read the working state
user        WORKING STATE, rendered from AgentState:
              task (repository, test command, bug report verbatim)
              initial test run: counts + one line per failing test (≤ 8)
              plan, suspects, hypotheses (active / tested / rejected), diagnoses
              what the runtime did, dated by step (reverts, continues; since #9)
              files read (path + line ranges only), searches made
              current patch: the diff, verbatim, clipped to 2,000 characters,
                marked tested / not tested yet (since #9)
              latest test run: fixed / still failing / newly failing, and which
                patch it ran on — current, reverted, or a previous version (since #9)
              budget left: steps, tool calls, test runs, tokens
...         the last `window_steps` (= 3) tool-calling steps, whole: the
            assistant message with its tool calls, the tool results, any nudge
user        the current phase's instructions (re-sent every step)
```

Decisions: K = 3 (Marco, from the simulation); the patch travels as a diff,
not as a file list; text-only replies (plan, hypothesis, analysis, summary) are
not replayed — their content is in the state. Whole steps are kept so
`tool_use` / `tool_result` pairs and OpenAI's reasoning items never split. The
ANALYZE instruction drops its copy of the test results (the state has them).
`full` mode is byte-for-byte what 2a sent; the two arms differ in context only.

### 8.3 Pre-registered expectations (Haiku, 2 × 14, same tools as 2a.1)

| metric | `full` (2a.1) | expected `compact` |
|---|---|---|
| tokens per task (median) | 56.2k | ≤ 35k |
| `cachetools_003` | 103k tokens, `budget_tokens`, regression left in | passes; < 45k tokens |
| `budget_tokens` terminations | 3 / 28 | 0 |
| success | 26 / 28, verdicts identical across repeats | ≥ 26 / 28, still identical |
| steps per task | 10.9 | may rise by 1–2 (re-reads of code that left the window) — reported as the cost |
| cost per task (median) | $0.063 | ≤ $0.045 |

Then Sonnet and luna once each: success unchanged (14 / 14), Sonnet tokens
−20–30%. If steps rise enough to eat the token saving, that is the result.
Not in 2b: prompt caching (a price lever, measured separately), a
threshold-triggered hybrid (a later variant), model-written summaries (§12.4).

### 8.4 First run (2026-09-15, `structured-2b-haiku45-x2-run1`) against §8.3

| metric | expected | first run | |
|---|---|---|---|
| tokens per task (median) | ≤ 35k | 38.8k (−31% vs. 56.2k) | ✗, close |
| `cachetools_003` | passes, < 45k | run 1 passes (16 steps, 62k); run 2 `budget_tokens` (24 steps, 104k) | ✗ |
| `budget_tokens` terminations | 0 | 1 / 28 | ✗ |
| success | ≥ 26 / 28, identical verdicts | 25 / 28, `cachetools_003` flips | ✗ |
| steps per task | 10.9, may rise 1–2 | 10.9 (tool calls 8.6 → 8.8) | ✓ |
| cost per task (median) | ≤ $0.045 | $0.048 (−24% vs. $0.063) | ✗, close |

Three failures. Two are `agent_loop` on `toolz_003`, a task the full arm solves
in both runs — an artifact of the runtime, not of the model: the loop detector
counted re-reads that the compact context had made necessary and refused them
with a notice that was false (issue #8). Fixed before the re-run: the call
signature carries the workspace version, only calls the model can still see
count, and re-reads of what left the window are tallied (`rereads`) instead of
refused. The third is `cachetools_003` again: one run passes where the full
arm had failed twice (a real effect of the smaller prompts — 62k against 101k
and 105k), the other re-localised twice (two forced hypotheses, 24 steps) and
hit the cap. That one is the model's, and stays in the table.

What compaction costs, measured: 32 repeated tool calls in 28 runs (13% of
calls) against 0 in the full arm, 8 forced hypotheses against 5, PATCH steps 71
→ 86 while LOCALIZE steps fell 198 → 185. Steps did not rise; the model spent
them differently.

### 8.5 Re-run after the fix (written before running)

Same command, same budget, `--repeat 2`. Expected: `toolz_003` passes both
runs (the artifact is gone); success ≥ 26 / 28; `agent_loop` 0; tokens median
≤ 40k; `budget_tokens` ≤ 1 (`cachetools_003` may still hit the cap — that is
the residual, reported as such); `rereads` > 0 (the price, now visible).
§8.3 stays the standard the arm is judged against; the re-run only removes the
artifact.

### 8.6 Second run (2026-09-18, `structured-2b-haiku45-x2-run2`)

| metric | §8.3 | §8.5 | second run | |
|---|---|---|---|---|
| tokens per task (median) | ≤ 35k | ≤ 40k | 34.8k (−38% vs. 56.2k) | ✓ ✓ |
| cost per task (median) | ≤ $0.045 | — | $0.043 (−32%; total $1.42) | ✓ |
| success | ≥ 26 / 28, identical verdicts | ≥ 26 / 28 | 27 / 28; `cachetools_003` flips | ✓ / ✗ identical |
| `toolz_003` | — | passes both | passes both — but both end `agent_loop` | ✓ / ✗ |
| `agent_loop` | — | 0 | 2 | ✗ |
| `budget_tokens` | 0 | ≤ 1 | 1 (`cachetools_003.1`, 26 steps, 101k, regression left in) | ✗ ✓ |
| `cachetools_003` | passes, < 45k | — | run 1 passes at 25 steps / 98k; run 2 hits the cap | ✗ |
| steps per task | 10.9, may rise 1–2 | — | 10.8 (tool calls 8.8, test runs 2.0) | ✓ |
| `rereads` | — | > 0 | 14 (of 21 repeated calls; 0 loop notices outside `toolz_003`) | ✓ |

The token and cost goals are met, and success is a point above the full arm.
Two things are not what §8.5 predicted. `toolz_003` passes both runs on the
patch in place, yet both runs end `agent_loop` — real loops this time, the
detector was right: after ANALYZE reverted a first attempt, the model applied
the correct fix and then read the same 26 lines four to six times in a row
("let me read the original dissoc function to see what needs to be fixed")
instead of ending its turn. The cause is the compact context again, one layer
up (issue #9): the runtime's one-time note "your change has been reverted"
had been folded into the phase instructions the compact arm re-sends every
turn, so after the model's new edit the last message in every prompt told it
the tree was original — while the state showed a patch and, unlabelled, the
failed run of the *reverted* patch. Fixed before the third run: the note is
an event dated in the state ("what the runtime did, by step"), the re-sent
instructions are bare, and the state now says which patch the latest test
run saw and whether the current one has been tested.

`cachetools_003` is the residual and it did not move: one run passes at 98k
(the full arm never passed it), the other spends 10 PATCH steps re-reading
`__touch` and the test without editing and hits the cap. Not a context
problem at a 35k median; the model does not reliably find the guard (`key in
self.__order`) from what it reads — two passes in the four compact runs so
far, none in the full arm's two.

### 8.7 Third run (written before running)

Same command. Expected: `toolz_003` passes both with `done` (`agent_loop` 0);
success ≥ 26 / 28; tokens median ≤ 36k; `cachetools_003` unchanged in kind
(may pass or hit the cap; `budget_tokens` ≤ 1). This is the last change to
the compact arm before the Phase 2 table: two fixes were both the runtime
telling the model something untrue under compaction (a false "its result is
above", a stale "your change has been reverted"); a third would start to look
like tuning the arm to Haiku on 14 tasks. If the third run does not clear
`agent_loop` on `toolz_003`, the arm is reported as it is.

### 8.8 Third run (2026-09-18, `structured-2b-haiku45-x2`) — the compact arm as it stands

| metric | §8.7 | third run | |
|---|---|---|---|
| `toolz_003` | passes both, `done` | passes both, `done`, 12 steps each (44.7k / 36.6k tokens; full arm 47.6k / 34.0k) | ✓ |
| `agent_loop` | 0 | 0 | ✓ |
| success | ≥ 26 / 28 | 26 / 28, identical verdicts across repeats (13 / 14 both, `cachetools_003` the failure both times — the full arm's exact pattern) | ✓ |
| tokens per task (median) | ≤ 36k | 38.1k (−32% vs. 56.2k) | ✗ |
| `budget_tokens` | ≤ 1 | 2: `cachetools_003.1` (22 steps, a last edit that broke 21 tests, unverified) and `cachetools_001` (19 steps, cap hit on the reply that carried the *right* edit — PASS) | ✗ |
| `cachetools_003` | unchanged in kind | both fail: one `no_progress` (the model added a guard, then removed it again and ended the turn with the already-tested diff), one at the cap | ✓ |

The two `toolz_003` runs are the fix working as designed: after the revert
the model claimed "I've made the fix" without editing (its step-5 edit and
its success result were still in the window), the runtime's no-edit nudge
sent it to the state, it read `dissoc` once, applied `try / except KeyError`,
ended the turn, green, FINALIZE "done". A window step whose effect a later
reset undid is the last inconsistency the compact prompt can carry; it cost
one nudge in each run and is left as is (§8.7).

What did not meet §8.7 is the token median, up from 34.8k to 38.1k, with two
`budget_tokens` instead of one. Both are Haiku's first-hypothesis luck on this
run, not the context: `cachetools_001` guessed `__contains__` / `__iter__`
before `expire` (a revert and a second LOCALIZE with five 80–140-line reads,
each ~2k tokens, so even a three-step window costs 5–8k per prompt), and
`toolz_001.1` needed three attempts (23 steps, 88k). Five workspace resets
and eight ANALYZE rounds against two and four in the full arm; the three
compact runs so far had 3 / 2 / 5 resets, so this is spread, not trend.

The arm against the full history, same tools, 2 × 14 each:

| | full (2a.1) | compact (third run) |
|---|---|---|
| success | 26 / 28, identical verdicts | 26 / 28, identical verdicts (same failing task) |
| steps / tool calls / test runs | 10.9 / 8.6 / 2.0 | 11.2 / 8.9 / 2.2 |
| tokens median / cost median / total | 56.2k / $0.063 / $1.82 | 38.1k / $0.046 / $1.49 |
| `budget_tokens` / `agent_loop` / `no_progress` | 3 / 0 / 0 | 2 / 0 / 1 |
| verified rate | 89.3% | 89.3% |
| repeated calls (re-reads after the window) | 0 (0) | 10 (9) |
| forced hypotheses / nudges / resets / ANALYZE rounds | 5 / 0 / 2 / 4 | 10 / 3 / 5 / 8 |
| steps by phase (localize / patch) | 198 / 71 | 179 / 96 |

Reading: compaction buys a third of the tokens and a quarter of the cost at
the same success, the same verified rate and the same failing task; the
price is 0.3 steps per task, nine re-reads in 28 runs and a few more
interventions. §8.3's ≤ 35k was met once (second run) and missed twice; the
honest number for the arm is 35–39k. `cachetools_003` stays the task neither
arm solves reliably (two passes in six compact runs, none in two full runs)
and it is not a context problem: the failing runs end with the model reading
`__touch` and the test for the fourth time, not with the cap alone.

## 9. Final Phase 2 comparison (written before running)

The three compact runs were separated by two runtime fixes, so they cannot be
pooled, and Haiku's spread between runs (25 → 27 → 26 of 28; 38.8k → 34.8k →
38.1k) is about the size of the differences being claimed. The Phase 2 table
therefore comes from one experiment on the final code, all arms on the same
tools, `--repeat 3` (42 runs per arm):

```
uv run python -m evals.runner --solver baseline   --model claude-haiku-4-5-20251001 --repeat 3 --max-run-cost 10
uv run python -m evals.runner --solver structured --model claude-haiku-4-5-20251001 --repeat 3 --max-run-cost 10
uv run python -m evals.runner --solver structured --context compact --model claude-haiku-4-5-20251001 --repeat 3 --max-run-cost 10
```

archived as `baseline-final-haiku45-x3`, `structured-final-full-haiku45-x3`,
`structured-final-compact-haiku45-x3` (about $4 + $2.7 + $2.2). The baseline's
code has not changed since 2a.1; it is re-run so that every column has the
same three repeats. Expected:

| | baseline | full | compact |
|---|---|---|---|
| success | 38–40 / 42, verdicts differ across repeats | 39 / 42 (`cachetools_003` ×3) | 39 / 42 (`cachetools_003` ×3, one lucky pass possible) |
| tokens median | ~100k | 50–60k | 35–40k |
| cost median | ~$0.11 | ~$0.06 | ≤ $0.047 |
| `budget_tokens` | ~40% of runs | ≤ 4 / 42 | ≤ 4 / 42 |
| `agent_loop` | — | 0 | 0 |
| steps | ~14 | ~11 | ~11 |

Then Sonnet and luna once each with `--context compact` (`structured-2b-sonnet5`,
`structured-2b-luna`): 14 / 14 both; Sonnet tokens −20–30% against its full
run; luna is the live test of the window with OpenAI reasoning items (whole
steps are kept, so each replayed assistant turn carries its own items — a 400
there is a bug to log, not a result). If any expectation fails, the table
shows it.

### 9.1 Results (2026-09-18, `structured-final-full-haiku45-x3`, `structured-final-compact-haiku45-x3`)

Marco ran the two structured arms back to back; the baseline column stays the
2 × 14 run of 2a.1 (its code has not changed).

| | expected full | full | expected compact | compact |
|---|---|---|---|---|
| success | 39 / 42 (`cachetools_003` ×3) | 40 / 42 (`cachetools_003` ×2) ✓ | 39 / 42 (one lucky pass possible) | 40 / 42 (`cachetools_003` ×1, `cachetools_001` ×1) ✓ on the count, ✗ on the names |
| tokens median | 50–60k | 57.6k ✓ | 35–40k | 38.4k ✓ |
| cost median | ~$0.06 | $0.066 ✓ | ≤ $0.047 | $0.047 ✓ |
| `budget_tokens` | ≤ 4 / 42 | 4 ✓ (2 of them PASS on the cap) | ≤ 4 / 42 | 2 ✓ |
| `agent_loop` | 0 | 0 ✓ | 0 | 0 ✓ |
| steps | ~11 | 11.2 ✓ | ~11 | 10.9 ✓ |
| verified on a green run | — | 90.5% | — | 95.2% |
| repeated calls (re-reads) | — | 2 (0) | — | 17 (17) |
| forced / nudges / resets / ANALYZE | — | 6 / 0 / 6 / 7 | — | 10 / 4 / 5 / 10 |
| invalid calls | — | 0.3% (1) | — | 1.7% (6) |
| steps by phase (localize / patch) | — | 301 / 114 | — | 253 / 145 |
| total cost | ~$2.7 | $2.76 | ~$2.2 | $2.14 |

Reading, for the README table: the two arms tie at 40 / 42; the compact arm
spends a third fewer tokens (−33%) and 29% less per task, ends more runs on a
verified green suite (the full arm hit the cap four times, twice with the
right patch already in), and takes the same number of steps. Its costs:
17 re-reads in 42 runs (every repeated call in the arm is one), four nudges
and ten forced hypotheses against none and six, and six invalid `read_file`
calls in which Haiku copied the state's `418-450` range notation into
`start="[418, 450]"` (one such call in the full arm, garbled differently) —
a prompt-format cost that the tool's error message repairs on the next step,
logged, not fixed. Neither arm is deterministic at three repeats.
`cachetools_003` passed 1 of 3 full runs (16 steps, at the cap) and 2 of 3
compact (23 and 14 steps, verified); across everything run since 2a.1 that is
1 of 5 full and 4 of 9 compact — suggestive that the smaller prompts leave
room to reach the guard, not a claim at this sample. The compact arm's other
failure, `cachetools_001`, went after `__contains__` / `__iter__` before
`expire`, kept the wrong patch, re-localised with 80–150-line reads and hit
the cap: the same "first hypothesis, then no room to recover" shape as
`cachetools_003`, on a task the arm had passed in its previous eight runs.

Against the baseline (2 × 14, 26 / 28, 101.6k, $0.111, 13 budget
terminations): the runtime with the full history cuts tokens and cost by
about 40% and budget terminations to a fifth at the same success; with
compaction the cuts are 62% and 58%. Phase 2's claim is therefore about
efficiency and reliability, not success rate: on v0 the success rate is set by
the model's first hypothesis on two cachetools tasks, and no arm moves it.

Still to run: Sonnet and luna once each with `--context compact`.

### 9.2 Sonnet and luna with the compact context (2026-09-18, `structured-2b-sonnet5`, `structured-2b-luna`)

Expected: 14 / 14 both; Sonnet tokens −20–30% against its full-history run;
luna a live test of the window with reasoning items.

| | Sonnet full (`structured-v0-sonnet5`) | Sonnet compact | luna full (`structured-v0-luna`) | luna compact |
|---|---|---|---|---|
| success | 14 / 14 | 14 / 14 ✓ | 14 / 14 | 14 / 14 ✓ |
| steps / tool calls / test runs | 6.9 / 3.9 / 2.0 | 6.9 / 3.9 / 2.0 | 8.1 / 9.2 / 2.1 | 8.3 / 10.4 / 2.1 |
| tokens per task (median) | 22.4k | 23.2k (+3%) ✗ | 39.0k | 32.5k (−17%) |
| input per call (median / p90 / max) | 3.2k / 5.6k / 8.3k | 3.2k / 5.4k / 6.7k | 4.9k / 8.6k / 9.5k | 2.5k / 8.3k / 12.4k |
| input tokens served from a prompt cache | 0 | 0 | 64% | 0% |
| output tokens (total, 14 tasks) | 13.7k | 16.5k | 11.2k (1.3k reasoning) | 13.7k (3.0k reasoning) |
| cost per task (median) / total | $0.0535 / $0.84 | $0.0537 / $0.85 | $0.0045 / $0.059 | $0.0074 / $0.103 (+64% / +75%) |
| invalid calls | 0.0% | 0.0% | 10.9% | 8.9% (13 × `edit_file(replacement=…)`) |
| failed edits / interventions | – | 0 / none | – | 3 / 2 forced hypotheses, 1 ANALYZE |
| model errors | 0 | 0 | 0 | 0 |

Two negative results, for two different reasons.

*Sonnet: nothing to compact.* Its runs are 6.9 steps, and the compact prompt
(system, a ~2k state, three whole steps, the instruction) is the same size as
a seven-step history: 3.2k input tokens per call, median, under either
context. Only the worst case moved (max 8.3k → 6.7k). The +3% is the state's
fixed overhead. Compaction pays when the history outgrows the window, which
on v0 happens to Haiku (11 steps, 4.6k → 3.6k per call) and not to Sonnet.
The −20–30% expectation was extrapolated from Haiku's step counts and was
wrong for Sonnet's.

*Luna: fewer tokens, higher cost.* Prompts shrank as designed (4.9k → 2.5k per
call, −17% per task) and the task cost 64% more. OpenAI caches prompt prefixes
automatically and bills a cache hit at a tenth of the input price; with the
full history, 64% of luna's input tokens were cache reads, because every
turn appends to a prefix the server has already seen. A working state
rebuilt on every step has no stable prefix, so the compact arm cached
nothing, and its uncached 432k input tokens cost more than the full arm's 540k
mostly-cached ones. Luna also reasoned more per step (reasoning tokens 1.3k →
3.0k over the run) — re-orienting on a fresh prompt each turn is not free.
Anthropic caches only on request and no Claude run here asked for it, so the
Haiku and Sonnet comparisons are between two uncached arms; the same effect
would appear there the moment the full arm sets `cache_control`.

The window itself worked with OpenAI's reasoning items: 116 calls, no 400, no
model error — whole steps carry their own items.

What this changes. The design was aimed at tokens per task (§8.1), and on
tokens compaction wins on two of three models; on cost per task it wins on
one (Haiku, uncached), ties on one (Sonnet) and loses on one (luna, cached).
Cost is the metric that matters, and cost depends on a lever the 2b design
deliberately left out (§8.3: "prompt caching — a price lever, measured
separately"). The two are not separable: caching favours an append-only
history, compaction destroys it. The next measurement is therefore the
full-history arm *with* caching on Anthropic (`cache_control` on the system
prompt and the last history block; automatic on OpenAI already) against the
compact arm, on Haiku, where compaction currently wins by 29% on cost. If
cached-full beats compact on cost, compaction's remaining case is the cap:
the runs the full history cannot finish under 100k tokens.
