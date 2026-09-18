# Issues log

Problems hit while building RepoPilot, with root cause and fix. One entry per
issue, newest first. The point is the same as the project's failure taxonomy:
a bug is only understood once its cause is written down and a test guards it.

Format: **symptom → root cause → fix → guard**.

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
