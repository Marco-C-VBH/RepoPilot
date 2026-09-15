# Why does everything pass on Bench v0? A leak audit

Date: 2026-09-14, after 74 baseline runs (Sonnet 5 ×3, Haiku 4.5 ×1 + 3 re-runs,
gpt-5.6-luna ×1). Question asked: is the near-100% success rate a harness bug
(the agent sees something it must not), or a property of the benchmark?

Short answer: **no harness leak was found; the ceiling is the benchmark's design.**

## 1. What the agent cannot see (checked in code)

| channel | status | where enforced |
|---|---|---|
| hidden test files | never in the agent's workspace or its test container; layered on in a *fresh* container at evaluation only | `evals/harness.py::evaluate_patch`, `evaluation_test_command` |
| gold patch, task JSON | live in `evals/benchmark/tasks/`, outside the exported repository tree | `Workspace.create` exports the target repo only |
| bug as a diff in git history | container: one synthetic commit, bug amended into it; host workspace: same since this audit (`git rev-list --count HEAD` == 1, pinned by a test) | `sandbox/docker.py::render_task_dockerfile`, `tools/workspace.py` |
| `.git` contents | refused by every path-taking tool; `search_code` walks `git ls-files` | `tools/paths.py::resolve_repo_path` |
| shell / arbitrary commands | no such tool; `run_tests` accepts only the task command or `pytest <existing paths / node ids>`, no options | `tools/toolbox.py::test_command_for` |

## 2. What the agent actually saw (checked in all 74 traces)

Script: scan every `tool_call` event's arguments and output for the hidden test
file paths and the hidden test function names of its task.

- Hidden test files or hidden test names in any tool call or output: **0 of 74**.
- Every `read_file` path lies inside the target repository. Test files read are
  the repository's own suites (`tests/test_tenacity.py` 30×, `tests/test_tlru.py`
  5×, ...), i.e. the *visible* fail-to-pass tests.
- One read stands out: two runs of `tenacity_004` read
  `releasenotes/notes/fix-reraise-tryagain-underlying-exc-*.yaml`, the upstream
  release note that describes, in prose, exactly the behaviour the mutation
  reverts. Nothing forbids that read — it is the repository's own documentation —
  but it means the answer was *in the repository*, see §3.

## 3. Why the tasks are easy anyway

Across the three models, the **first `edit_file` landed in a gold file in 71 of
71 runs that edited**. Localization never failed once, through two different
channels:

| model | first tool call | first query verbatim from the report | first file read is gold | ran tests before first edit |
|---|---|---|---|---|
| claude-sonnet-5 (43 runs) | `search_symbol` 38, `search_code` 3, `read_file` 2 | 41 / 43 | 40 / 43 | 0 / 43 |
| claude-haiku-4-5 (17 runs) | `run_tests` 16 | 1 / 17 | 4 / 17 | 17 / 17 |
| gpt-5.6-luna (14 runs) | `run_tests` 8, `search_symbol` 5 | 5 / 14 | 13 / 14 | 13 / 14 |

Sonnet localizes from the **bug report** (the identifier in the report goes
straight into `search_symbol`); Haiku and luna localize from the **failing
tests** (12 of 14 tasks have visible fail-to-pass tests whose names and assertion
messages point at the function). Both channels are leaks by construction:

1. **Identifiers in the report.** Every description names the class, function or
   decorator involved (`dissoc`, `TTLCache`, `wait_exponential`, `reraise=True`).
   The difficulty tiers only vary how much *behaviour* is described.
2. **Visible fail-to-pass tests.** A mutation in a well-tested library breaks the
   library's own tests, and the agent may run them. SWE-bench hides its
   fail-to-pass tests for exactly this reason. Only `toolz_003` / `toolz_004`
   are hidden-only, and both still pass because of (1) and (3).
3. **Self-describing mutation sites.** The code around the mutation still states
   the correct behaviour: `tenacity_004`'s change sits two lines below a comment
   saying "surface the underlying exception ... rather than the opaque TryAgain
   sentinel"; `toolz_003`'s docstring says missing keys are ignored (the report
   even quotes it); `cachetools_003`'s class is called `LRUCache`. A mature
   library's comments, docstrings, tests and release notes are an oracle for any
   mutation that reverts documented behaviour.
4. **Textbook anti-patterns.** Several mutations are patterns models are trained
   to flag on sight: `cache={}` as a default argument (`toolz_004`), `if not
   cache` for an emptiness-vs-None check (`cachetools_005`), `type(e) in types`
   instead of `isinstance` (`tenacity_003`), an unguarded `del d[key]`
   (`toolz_003`), `>` vs `>=` on a `stop_after_attempt` (`tenacity_001`).
5. **Memorized libraries.** cachetools, tenacity and toolz are widely depended-on
   and certainly in every model's training data; the model may simply recall the
   original line. Not measured yet (see §4).

None of this is a defect of Phase 0: the harness's job was to be valid (null
solver 0 / 14, gold 28 / 28 deterministic), and it is. It is the reason the
success rate cannot separate agent architectures on v0, while efficiency,
termination and invalid-call metrics can (README, "Phase 1 findings").

## 4. Proposed ablation (cheap, decisive)

Four runs of the same baseline on the same 14 tasks, ~$0.05 each with luna and
~$0.80 each with Sonnet, with two runner switches:

| arm | bug report | `run_tests` tool | measures |
|---|---|---|---|
| A | full | yes | today's baseline |
| B | replaced by "The test suite has failures; find and fix the bug." | yes | contribution of leak (1) — the agent must localize from tests |
| C | full | no (hidden-only conditions for every task) | contribution of leak (2) |
| D | "There is one injected bug in this repository; find and fix it." | no | leaks (3)–(5) alone: is the mutation detectable from code? |

Expected reading: if D stays high, the mutation sites themselves give the bug
away and v1 must change *what* is mutated (sites without a descriptive comment or
release note, non-textbook faults, multi-site and cross-module changes, at least
one repository the models are unlikely to have memorized). If D drops but B or C
stays high, the fix is in the task packaging (symptom-level reports, hidden
fail-to-pass tests). The outcome also decides how much of Phase 3's retrieval
work could show up on success rate at all.

Memorization on its own can be probed without the harness: ask the model, with
no tools, to write `tenacity.wait_exponential.__call__` or `toolz.dissoc` from
memory and compare with the pinned source.

## 5. Guards added by this audit

- `Workspace.create` amends the bug into the single base commit (previously a
  second "bug" commit), matching the sandbox image; `tests/test_tools.py`
  asserts the one-commit history.
- The trace scan of §2 is worth re-running after any change to the tools or to
  the task format; it is a 40-line script over `results/*/traces/*.jsonl` and
  `evals/benchmark/tasks/*.json` (hidden file paths from `hidden_test_patch`,
  hidden test names from its `+def test_...` lines).
