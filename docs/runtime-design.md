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
- *Loop detection.* Signature = tool name + canonical JSON of the arguments.
  Third occurrence → refused with a replanning message
  (`loop_interventions`); fourth → `agent_loop`.
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
