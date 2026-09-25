# Issues log

Problems hit while building RepoPilot, with root cause and fix. One entry per
issue, newest first. The point is the same as the project's failure taxonomy:
a bug is only understood once its cause is written down and a test guards it.

Format: **symptom → root cause → fix → guard**.

---

## 13 · The sandbox's pytest drifted and turned a target repository's test file into a collection error

**Date:** 2026-09-25 · **Area:** sandbox × task authoring · **Severity:** medium
(two of nine click tasks could not be built; would have hit every later task
whose test command touches the same file)

**Symptom.** `make_task evals/benchmark/sources/click_005` (and `click_007`)
stopped at the buggy run with `collection errors: ['tests/test_basic.py']`,
while the same command, files and report plugin collected and ran cleanly in
the cloud pre-check. The seven other click tasks built; their test commands do
not include `tests/test_basic.py`.

**Root cause.** The base image installed `pytest>=8`, i.e. whatever was
newest on the day the image was built. The click images were built on
2026-09-23 and got pytest 9.1, which emits `PytestRemovedIn10Warning` at
collection for a `parametrize` whose values are an iterator —
`tests/test_basic.py::test_boolean_conversion` passes an `itertools.chain`.
click's `pyproject.toml` runs its suite with `filterwarnings = ["error"]`, so
the warning became a collection error for the whole file. The cloud pre-check
runs pytest 9.0.3, which does not warn. Nothing in the task was wrong; the
sandbox's toolchain was unpinned, so the derivation depended on the build date.
The first symptom was also unreadable: `_run_with` reported the failing file
without the reason the plugin had recorded.

**Fix.**

- `docker/base.Dockerfile` pins `pytest==9.0.3` — the version the cloud
  pre-checks use and the version every shipped test list was derived under.
  The base-image tag hashes the Dockerfile, so the base and every task image
  rebuild on the next run; the derived lists do not change.
- `evals/benchmark/authoring.py` (`_run_with`): a collection error now prints
  the plugin's recorded reason (longrepr or message) for each failing file.

**Guard.** The pin itself: a pytest bump is now a deliberate edit that rebuilds
every image, not a side effect of the calendar. `docs/benchmark-authoring.md`
records the version the cloud pre-check must match.

**Lesson.** A sandbox that installs "the latest" of anything is not a fixed
environment; the target repositories' own test configuration (warnings as
errors) turns toolchain drift into failures that look like task bugs. Pin what
the derivation depends on, and make every refusal carry its reason.

## 12 · The compact state dropped the retrieved evidence exactly when the model needed its text

**Date:** 2026-09-19 · **Area:** retrieval × compact context · **Severity:** low
(no failure; about one wasted step per run in the `evidence` arm)

**Symptom.** Phase 3's first agent-level runs (`structured-p3-evidence-haiku45-x2`
against `-tool-`): with the report's top chunks shown at PLAN, LOCALIZE
shrank as designed (195 → 145 steps over 28 runs; `search_symbol` calls 39 →
6) but PATCH grew by almost as much (85 → 112), and tokens per task did not
move (41.3k → 41.2k). In the traces, 28 of the evidence arm's 33 PATCH-phase
`read_file` calls re-read a range the evidence block had shown — the model's
first PATCH action in 9 runs was to read the function it had just hypothesised
about from the evidence.

**Root cause.** My design: the evidence was rendered into the working state
"during PLAN and LOCALIZE" and dropped in PATCH, on the theory that by then the
model had read what it needed. It had not read it — it had *seen* it, in the
evidence, and stated its hypothesis from that (LOCALIZE with zero tool calls in
several runs). At PATCH the block was gone, `edit_file` needs the exact text,
and the model paid a step to get it back.

**Fix.** The evidence stays in the state while there is no patch in place
(`not diff.strip()`): PLAN, LOCALIZE and PATCH up to the first edit. After an
edit the model has the exact text in its window and the diff in the state;
after an ANALYZE revert the tree is original again and the block returns. The
rule is stateless and says what it means: "here is what retrieval found, until
you have changed something".

**Guard.** `tests/test_retrieval.py::test_evidence_mode_retrieves_for_the_report_and_offers_the_tool[compact]`
asserts the block is present at the PATCH step before the edit and absent
after it. The re-run (`docs/retrieval-design.md` §9, §10) measured the
effect: PATCH reads repeating the evidence 28 of 33 → 2 of 11, PATCH steps
4.0 → 2.9 per run (control 3.6); tokens per task did not fall below the
control's at the median — the block's cost on every call before the first
edit is the reason, and that is the pre-registered fallback conclusion, not a
bug.

**Lesson.** The third entry in the same family as #8 and #9: the compact
context is rebuilt every step, so every rule about what it shows is a rule
about what the model *knows*. "Which phase we are in" was the wrong condition;
"whether the model has anything to edit yet" was the real one.

---

## 11 · Whitespace-tolerant edits mis-indented inserted lines and would not let the model fix them

**Date:** 2026-09-19 · **Area:** tools (`edit_file`, Phase 2a.1) · **Severity:**
medium (one `budget_tokens` run in the Phase 3 experiment; every tolerant edit
since 2a.1 left a mis-indented block — 13 of 13 in the last four runs)

**Symptom.** `toolz_003`, `evidence` arm, run 1: the correct fix was in the
tree by step 13, and the run went on to step 25 and the token cap — three more
edits of the same five lines (steps 14, 16, plus five refused at the PATCH
visit's cap of four), each read back showing `if key in d2:` at 13 spaces
under a `for` at 8, and `del d2[key]` at 17. The harness passed the patch
(Python accepts a block indented by any amount deeper than its parent), so
the run counts as PASS on the cap; it cost $0.123 and 25 steps for a
two-line change. Across the four most recent runs, all 13 whitespace-matched
edits left a region whose indentation was not a multiple of four.

**Root cause.** Two rules in `_reindent`. (1) A changed or inserted line was
shifted by the *first line's* offset between the model's `old_string` and the
file. In the trace the model's first line was right (4 spaces, delta 0) and
every following line was off by one (9 for 8, 13 for 12); the kept lines were
corrected to the file's indentation, the inserted `if key in d2:` kept its
13. (2) A kept line always took the file's indentation, so when the model
then tried to repair the damage with an edit that changed nothing but
whitespace — three times, with the right absolute indentation in
`new_string` — the tool wrote the file's wrong indentation back every time.
The tolerance that 2a.1 added for a model that miscounts whitespace could not
distinguish a miscount from a correction, and defaulted to the file even when
the file was what the model was fixing.

**Fix.** `_reindent` now places a changed or inserted line relative to the
nearest kept line *above* it, by the offset the model gave it in `new_string`
relative to that line (the anchor's own offset error cancels out); a kept
line the model moved relative to its neighbour (a statement pulled under a
new `if`) moves the same amount from the file's indentation; and an edit whose
lines are all kept — only whitespace changed — is a deliberate
re-indentation, written as given and aligned to the file's first line. Every
whitespace-matched edit from the trace now yields 4 / 8 / 8 / 12 / 16.
Separately, the runtime no longer waits for a turn the model may never end
once a PATCH visit's edits are spent and the tree has changed: it runs the
tests itself (a forced transition, noted in the state — "the 4 edits of this
PATCH visit were used, so the runtime ran the tests"). In this trace that
would have ended the run at step 17 instead of 25.

**Guard.** `tests/test_tools.py::test_edit_file_reindents_inserted_lines_from_the_nearest_kept_line`
and `::test_edit_file_honours_a_pure_reindentation` replay the trace's three
edits verbatim against the file; the 2a.1 cases still pass.
`tests/test_runtime.py::test_patch_edit_limit_refuses_further_edits_and_the_runtime_tests`
covers the forced run and the case where the cap is spent without changing
the tested tree (no run; the model must end its turn). Phase 3.1 (56 runs,
`docs/retrieval-design.md` §10): four whitespace-matched edits, two of them
this issue's exact shape, all four regions correctly indented; the forced run
never fired — no PATCH visit used its four edits.

**Lesson.** A tolerance is a model of the other side's mistakes. 2a.1's said
"the model's whitespace is off by a constant"; the trace says "off by a
constant from the second line on", and "sometimes the model is the one who is
right". Both were visible in the first 2a.1 traces as 13-space blocks that
happened not to matter — the tolerance's own output should have been checked
for consistency (indentation a multiple of the file's unit) before the fix
was declared done.

---

## 10 · The local-embedder test asserted a judgement, not a wiring

**Date:** 2026-09-18 · **Area:** retrieval / tests · **Severity:** low (test only;
the dense channel worked: 384-dimensional unit vectors, model downloaded and
loaded on the first call)

**Symptom.** `tests/test_retrieval_local.py` (marker `embeddings`, the first
run with `fastembed` installed) failed on its last line: for the query
"evicting one entry too late when the cache is full" over the demo file's six
chunks, `bge-small` ranked the *module* chunk first — `DEFAULT_SIZE = 3`, one
line — ahead of `Cache.put`, the method that evicts.

**Root cause.** What is embedded is the chunk's title plus its text. For a
one-line chunk the title (`demo/cache.py module (module)`) is most of the
input, and its path tokens (`cache`) match the query better than `put`'s body
does, which never says "cache", "evict" or "full" — it says `pop`, `size`,
`items`. A small embedding model does not bridge that gap on a toy file, and
the test had asserted it would ("the method is the top hit"). The assertion
encoded a hope about the model, not a property of the code.

**Fix.** The test checks the seam: vector shape and normalisation, a
deterministic query embedding, and a *relative* ordering — `put` above its
sibling `get` for a description of what `put` does. The order is in the
assertion message so the next mismatch is readable without a round trip.

**Guard.** The question the failed assertion was really asking — how good is
the dense channel on this benchmark — is answered by `scripts/retrieval_eval.py`
on the 14 tasks, per channel, against gold files and symbols, and is
pre-registered as the weakest channel (`docs/retrieval-design.md` §5). If
module chunks crowd out functions there too, the fix is in the embedding
text (drop the path for short chunks, or embed body first), decided by that
measurement, not by a toy.

**Lesson.** A unit test may assert what code does; what a model *judges* is a
measurement with a sample size, and belongs in the evaluation, not the test
suite. Tests of a model seam assert shapes, determinism and orderings that
are robust by construction.

---

## 9 · Compact context re-sent "your change has been reverted" after the model had edited again

**Date:** 2026-09-18 · **Area:** structured runtime, compact context · **Severity:**
medium (both `toolz_003` runs of the second 2b experiment ended `agent_loop`,
with the right patch in place)

**Symptom.** Re-run after issue #8 (`results/structured-20260918-034526-3e11`):
27 / 28, but `toolz_003` still ended `agent_loop` twice — this time on a real
loop, with the correct fix already in the tree (the harness judged both runs
PASS). Run 2, after ANALYZE reverted the first patch at step 7: the model
re-read `dissoc` (step 8), applied the right fix (step 9), then read the same
range at steps 10, 11, 12, 13, 14, 15 — "let me read the original dissoc
function to see what needs to be fixed" — until the loop detector ended the
run. It never ended the turn, so the runtime never ran the tests. Run 1 was
the same shape with one more detour (steps 16–19).

**Root cause.** The runtime attaches a note to the next phase's instructions
when something happened between phases — here "Your change has been
reverted; the repository is back to its original state." With the full
history that note appears once, in order, before the model's next edit. The
compact context re-sends the current instructions on every turn as the last
user message, and `instruct()` had stored the note-prefixed text as those
instructions: after the model's new edit at step 9, every later prompt still
ended with "your change has been reverted" while the WORKING STATE above it
showed a current patch and a failed "latest test run with your change" (a run
of the *reverted* patch, unlabelled). The model believed its edit was gone,
went looking for "the original" code, found its own patch, and asked again.

**Fix.** Events and instructions are different things. `instruct(instructions,
note)` appends the note once to the full history (unchanged for the `full`
arm) and re-sends only the bare instructions. The event goes into the state,
dated: `AgentState.notes` ("step 7: at your request the change tested at step
6 was reverted; the tree was back to the original"; likewise a FINALIZE
"continue"), rendered as "What the runtime did, by step". The state also says
what the latest test run is evidence about: "on the current patch", "on a
change since reverted (the tree is back to the original)" or "on a previous
version of your change (the current patch has not been tested)", decided by
comparing the current diff with the diff at that run; the patch header carries
"tested, result below" / "not tested yet".

**Guard.** `tests/test_context.py::test_compact_context_keeps_runtime_events_in_the_state_not_the_instruction`
(revert, then a new edit: the re-sent instruction never mentions the revert,
the state dates it, the test section flips from "since reverted" to "a
previous version") and the rendering cases in
`test_render_state_carries_task_reads_patch_tests_and_budget`.
`tests/test_runtime.py::test_failing_verification_goes_through_analyze_and_can_revert`
keeps the full arm's note in place.

**Lesson.** Same family as #8: compaction turns "what was said once" into
"what is said every turn", so anything the runtime tells the model must
either be true at every step it is repeated at, or be dated. The fix was not
a new prompt but the distinction the runtime already had in its own records —
which patch a test run saw — surfaced where the model reads it.

---

## 8 · Loop detector counted re-reads the compact context had made necessary

**Date:** 2026-09-15 · **Area:** structured runtime, compact context · **Severity:**
medium (both `toolz_003` runs of the first 2b experiment ended `agent_loop`)

**Symptom.** First `--context compact` run with `claude-haiku-4-5`
(`results/structured-20260915-001158-b6ad`, K = 3): `toolz_003` failed twice,
both as `agent_loop`, on a task the full-history arm solves every time. Run 1:
the model read `toolz/dicttoolz.py` 201–226 at steps 3 and 4, its first patch
failed, ANALYZE reverted it (workspace reset at step 8), and the re-read at step
9 was refused with "you have already made this exact call, and its result is
above" — a third occurrence in the detector's count, but nothing of it was in
the prompt: steps 3–4 had left the window and the tree had just been reset.
Step 12 repeated it and the run ended. Run 2: the same range read at steps 3,
8 (after a reset) and 16 (after failed edits); the notice at 16 was equally
untrue, the repeat at 18 terminated the run.

**Root cause.** Two assumptions of the Phase 2a loop detector stopped holding
under compaction. (1) "An identical call has an identical answer": false once
the workspace changes — after an edit or a reset, the same read is a new
question. The 2a runs rarely tripped on this because the full history kept the
old answer in view and the model rarely re-read. (2) "The earlier result is
still in the model's context": the compact context drops tool outputs older
than K steps *by design*, and its own note tells the model to read again what
it needs. The detector counted every occurrence since the start of the run, so
the runtime was refusing the very re-reads its context strategy asks for, with
a message that was false when it mattered.

**Fix.** `repopilot/agent/policies.py` / `runtime.py`: the call signature
includes the workspace version (bumped by every successful edit and every
reset), and only occurrences the model can still see count — steps at or after
the start of the compact window; everything, as before, with the full history.
The third *visible* identical call is refused with the notice (now always
true); repeating it while the notice is still in view ends the run (the
separate `loop_terminate_at` limit is gone — that was its only meaning). A
repeat whose earlier result had left the window executes normally and is
tallied as `rereads` in the runtime record and the run metrics: the price of
compaction, now measured instead of punished.

**Guard.** `tests/test_runtime.py::test_the_same_read_after_an_edit_or_a_reset_is_not_a_repeat`
(full history: the same read at three workspace versions, all executed) and
`tests/test_context.py::test_compact_context_rereads_outside_the_window_are_not_loops`
(K = 2: re-reads after the window execute, three in view earn the notice, the
notice expires with the window) plus
`..._still_ends_a_loop_the_model_can_see` (a visible loop still terminates).

**Lesson.** A guard written for one context strategy encodes that strategy's
assumptions. Compaction changed what "the model already knows" means, and every
runtime rule phrased in those terms — here, the loop detector — had to be
re-derived from what is actually in the prompt. The rule now says what it
checks: not "this call was made before" but "this call was made before, with
the same tree, where the model can see it".

---

## 7 · Runtime dropped the edit that arrived in the reply crossing the token cap

**Date:** 2026-09-15 · **Area:** structured runtime · **Severity:** medium (turned
one solvable task into a `budget_tokens` failure)

**Symptom.** First `--solver structured` run with `claude-haiku-4-5`
(`results/structured-20260914-212220-be5f`): `tenacity_004` ended
`budget_tokens` at step 16 with no patch. The trace shows step 16's reply
carrying an `edit_file` call on `RetryError.reraise` — the right function and,
by its text, the right change (`__cause__ or __context__`) — followed directly
by `run_end`: no `tool_call` event, no diff.

**Root cause.** `_Execution.model_call` checked the token / cost limits
immediately after recording the reply and returned the termination *before*
the reply's tool calls were executed. The baseline runs the tool loop first and
checks the cumulative limits after it; the runtime inverted the order, so any
run that crossed the cap on a reply with tool calls lost that reply's work.
The tokens were already spent either way; executing the calls costs nothing.

**Fix.** `repopilot/agent/runtime.py`: `model_call` only terminates on
tokens / cost when the reply has no tool calls; when it has, the phase loop
executes them and then asks `spent()`. A run that crosses the cap mid-turn still
ends as `budget_tokens` (no verification run), but its last edit is in the
workspace and the harness judges it — the same semantics as the baseline, where
three of Haiku's `budget_tokens` runs passed on the patch they had in place.

**Guard.** `tests/test_runtime.py::test_edit_in_the_reply_that_crosses_the_token_cap_is_still_applied`
scripts exactly this: the reply that crosses the cap carries the edit; the
patch must contain it and the termination must still be `budget_tokens`.

**Lesson.** A budget check has a *position* in the loop, and the position is
part of the budget's semantics. "Check after the model call" and "check after
the step" differ by exactly the work the model just asked for. The first run of
a new control loop should be read trace by trace before its numbers are
compared with anything.

---

## 6 · Live test for `tool_choice="none"`: the model obeyed, the test contradicted itself

**Date:** 2026-09-15 · **Area:** model layer / tests · **Severity:** low (test only;
the runtime feature it covers worked on both providers)

**Symptom.** The Phase 2a round trip added to `tests/test_models_live.py` — tool
calls in the history, a user turn right after the tool result, tools defined
but `tool_choice="none"` — was accepted by both APIs and produced no tool call,
which is what the runtime's decision points need. It then failed on the
answer: asked "Now 3 + 4?", `claude-haiku-4-5` returned an *empty* reply and
`gpt-5.6-luna` said "I can't calculate that right now."

**Root cause.** The test's own instructions were contradictory. Its system
prompt says "Use the add tool for arithmetic; never compute it yourself", the
call then disabled the only tool and asked for arithmetic. Both models obeyed
the system prompt, each in its own way: luna refused in words, Haiku returned
nothing at all. The mechanism under test (definitions sent, tool use forbidden)
was fine; the question was not.

**Fix.** The decision-point turn asks for something that needs no tool ("Reply
with the single word: done") and checks the reply text; the assertion message
carries the whole response so the next mismatch is readable.

**Guard.** The runtime's decision-point prompts (PLAN, ANALYZE, FINALIZE) never
conflict with the system prompt: they say "No tools in this phase. Reply with
JSON only", and the runtime tolerates an empty reply at each of them (a plan
falls back to the raw text or nothing, an analysis to "patch, keep the patch",
a finalize to "done"; in LOCALIZE an empty reply is nudged once). The
observation that a model may answer a contradictory instruction with an *empty*
message, not a refusal, is why those fallbacks stay.

**Lesson.** When a test disables a capability, do not also ask for the thing
only that capability can do; a model that complies with the stronger
instruction will look broken. And an empty reply is a real failure mode to
handle, not just a refusal.

---

## 5 · OpenAI live test: function tools rejected on Chat Completions

**Date:** 2026-09-14 · **Area:** model layer · **Severity:** medium (blocked every
OpenAI tool call)

**Symptom.** `uv run pytest -m llm` passed for Anthropic and failed for OpenAI on
the first call that offered a tool:

```
openai.BadRequestError: Error code: 400 - Function tools with reasoning_effort are not
supported for gpt-5.6-luna in /v1/chat/completions. To use function tools, use
/v1/responses or set reasoning_effort to 'none'.
```

The plain text call before it had succeeded, so the key, the model id and the
request plumbing were fine; only tools were refused.

**Root cause.** The adapter used the Chat Completions endpoint. The gpt-5.x
models reason by default, and Chat Completions cannot combine reasoning with
function tools -- OpenAI's supported path for that is the Responses API.
Turning reasoning off (`reasoning_effort="none"`) would have made the request
succeed but would have benchmarked a deliberately weakened model against
Claude at its defaults, which is not a fair provider comparison.

**Fix.** `repopilot/models/client.py`: `OpenAIClient` now speaks the Responses
API (`responses.create` with `instructions`, `input` items, flattened function
tools with `strict: False`, `max_output_tokens`, `store=False` +
`include=["reasoning.encrypted_content"]`). Reasoning models keep state in
their output items, so `ModelResponse.raw_items` carries each turn's items and
`OpenAIClient.to_input` replays them verbatim on the next call (the neutral
`Message` gained an opaque `raw_items` field; the Anthropic adapter ignores it).
`Usage` gained `reasoning_tokens`. The live test and the smoke script also got
realistic output budgets (a 20-token cap is not enough for a model that thinks
before it answers).

**Guard.** `tests/test_models_live.py` (`-m llm`) is exactly the test that caught
this: one paid round trip per provider, run on purpose before a commit that
touches the adapters. The stand-in tests in `tests/test_model_clients.py` pin
the new request shape.

**Lesson.** Stand-in tests check that we send what we *think* the API wants;
only a live call checks what it *actually* wants. Keep one cheap live test per
provider and run it whenever an adapter changes.

---

## 4 · CI red: ruff wanted to reformat a task description

**Date:** 2026-09-10 · **Area:** CI / benchmark data · **Severity:** low

**Symptom.** `ruff format --check` failed on
`evals/benchmark/sources/cachetools_005/description.md` — a Markdown file. It
wanted blank lines around the `@cached` function in the Python code fence.

**Root cause.** Recent ruff versions format Python code blocks inside Markdown
files, and `description.md` is discovered like any other file. But a task
description is *data*: it is the bug report the agent sees, stored verbatim in
the task JSON. Rewriting it (even harmlessly) makes the source directory and
the generated task drift apart, and the formatter has no business editing prose.

**Fix.** `pyproject.toml`: `[tool.ruff] extend-exclude = ["evals/benchmark/sources", "docs"]`.
Task sources and hand-written docs are never touched by the formatter or the
linter; code fences there are illustrative and may be deliberately unformatted.

**Guard.** CI. Lesson: when a tool gains a new file type, check that it is not
now reading directories that hold data.

---

## 3 · CI red on `ruff format --check` after the runner commit

**Date:** 2026-09-09 · **Area:** CI / tooling · **Severity:** low

**Symptom.** GitHub Actions failed at `uv run ruff format --check .`:
`tests/test_sandbox_docker.py:41` had three blank lines before a top-level
`@pytest.fixture` where the formatter allows two. Tests were green; only the
format gate was red.

**Root cause.** The fixture constants were moved out of that file into
`tests/fixture_repo.py` with a scripted text edit that removed the block but
left its surrounding blank lines behind. The pre-delivery `ruff format --check`
still reported the file as formatted because ruff's cache entry for it was
stale (the file lives on a network mount whose mtimes ruff's cache keys on), so
the unformatted file shipped.

**Fix.** `ruff format tests/test_sandbox_docker.py`; the pre-delivery check now
runs with `--no-cache`.

**Guard.** CI itself is the guard — it caught the problem. Lesson for scripted
refactors: re-run the formatter on the touched file, and never trust a cached
formatter verdict for a file that was just rewritten out-of-band.

---

## 2 · Docker integration tests skipped silently

**Date:** 2026-09-09 · **Area:** tests / sandbox · **Severity:** low

**Symptom.** `uv run pytest -m docker -v` reported `7 skipped` with no explanation,
even though Docker Desktop was installed. It was simply not running yet, but the
skip message ("no Docker daemon reachable") did not say *why* the probe failed,
and `-v` does not print skip reasons at all (`-rs` does).

**Root cause.** `docker_available()` collapsed every failure mode (CLI missing,
daemon down, timeout) into a bare `False`.

**Fix.** `repopilot/sandbox/docker.py`: new `docker_status() -> (available, detail)`
that returns the exact reason (CLI path, daemon error text, timeout);
`tests/conftest.py` puts that detail into the skip reason. `docker_available()`
is now a thin wrapper over it.

**Guard.** None needed — the diagnostic is the fix. Run Docker tests with `-rs`
to see skip reasons.

---

## 1 · pytest inside the sandbox could not read the report plugin

**Date:** 2026-09-09 · **Area:** sandbox image build · **Severity:** high (blocked
every `run_tests`)

**Symptom.** `test_gold_patch_turns_failing_tests_green_and_hidden_test_passes`
failed with `report_found == False`; the container's stderr ended in

```
PermissionError: [Errno 13] Permission denied: '/opt/repopilot/repopilot_pytest_plugin.py'
```

raised from pytest's assertion-rewrite import of the plugin. The other six
sandbox tests passed because none of them ran pytest.

**Root cause.** A chain of preserved file modes. The plugin file was written to
the working tree with mode `0600` (files delivered through the Cowork desktop
bridge land as owner-read/write only). `ensure_base_image` staged it into the
Docker build context with `shutil.copy`, which copies permission bits, and the
Dockerfile's `COPY` preserves the source mode as well. Result inside the image:
`/opt/repopilot/repopilot_pytest_plugin.py` owned by root with mode `0600` —
unreadable by the unprivileged `runner` user that executes pytest. The
root-ownership was deliberate (tamper resistance); the mode was an accident.

**Fix.**

- `docker/base.Dockerfile`: after the `COPY`, `RUN chmod 755 /opt/repopilot &&
  chmod 644 /opt/repopilot/repopilot_pytest_plugin.py` — world-readable, still
  root-owned.
- `repopilot/sandbox/docker.py` (`ensure_base_image`): use `shutil.copyfile`
  (content only, no mode bits) and `chmod(0o644)` on the staged copy, so a
  restrictive checkout mode can never reach the image again.

Because the base-image tag hashes the Dockerfile and the plugin source, the fix
rebuilt the base image automatically on the next run; the stale
`repopilot-base:3.11-<old hash>` can be removed with `docker rmi`.

**Guard.** `tests/test_sandbox_docker.py::test_snapshot_is_buggy_unprivileged_and_history_free`
now asserts that `runner` can `cat` the plugin file, so a permissions regression
fails in the first Docker test rather than surfacing as a mysterious
"no report" in `run_tests`.

**Lesson.** Anything copied into an image for another user to execute needs an
explicit mode; never rely on the mode the file happened to have on the host.
