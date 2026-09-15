# Issues log

Problems hit while building RepoPilot, with root cause and fix. One entry per
issue, newest first. The point is the same as the project's failure taxonomy:
a bug is only understood once its cause is written down and a test guards it.

Format: **symptom → root cause → fix → guard**.

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
