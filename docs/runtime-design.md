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
              files read (path + line ranges only), searches made
              current patch: the diff, verbatim, clipped to 2,000 characters
              latest test run with the patch: fixed / still failing / newly failing
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
