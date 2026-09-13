# Authoring RepoPilot-Bench tasks

Phase 0's last step: 10–20 controlled-mutation tasks on real, small, pure-Python
repositories. A task is a bug you inject into a pinned commit, the bug report the
agent sees, and the tests that decide whether a fix is correct. You write the four
human parts; `scripts/make_task.py` derives everything else by actually running
the code, so nothing in a task file is typed by hand.

## Target repositories

All three are pure Python with no runtime dependencies, test suites that run in
under five seconds, and permissive licenses. Every task in Phase 0 pins one of
these commits.

| repo | pinned commit | install | tests | natural categories |
| --- | --- | --- | --- | --- |
| [cachetools](https://github.com/tkem/cachetools) v7.1.8 | `4500e3d04288738d25acbb4973eb3c3e1bf41db9` | `pip install -e .` | `tests/` (14 files, 333 tests, 4.6 s) | cache_invalidation, state_management, off_by_one |
| [toolz](https://github.com/pytoolz/toolz) 1.1.0 | `568c2b8393973cd172a466546c9d95779c452438` | `pip install -e .` | `toolz/tests/` (180 tests, 0.9 s) | off_by_one, wrong_condition, missing_check |
| [tenacity](https://github.com/jd/tenacity) 9.2.0 | `a2af454834c6bb5a1e39d67334031cdaf0f475b5` | `pip install -e .` | `tests/` (178 tests, 2.6 s without `test_tornado.py`) | retry_logic, exception_handling, wrong_condition |

Notes from the survey:

- cachetools uses a `src/` layout (`src/cachetools/__init__.py` holds every cache
  class; `_cached.py` / `_cachedmethod.py` hold the decorators). Tests are
  unittest-style classes, so node ids look like `tests/test_lru.py::LRUCacheTest::test_lru`.
- toolz keeps its tests inside the package (`toolz/tests/`). `toolz/tests/test_package.py::test_has_version`
  needs the package installed, which the sandbox does. Its pytest config turns
  every warning into an error (`filterwarnings = error`), so a hidden test that
  triggers a `DeprecationWarning` fails on both sides and `make_task` rejects it.
  Doctests are only run through `test_curried_doctests.py`, and only for the
  curried objects, so a docstring example is not a visible test.
- tenacity's `tests/test_tornado.py` needs tornado; never put it in a
  `test_command` and the plain `pip install -e .` is enough. Avoid tests that use
  real sleeps (grep for `time.sleep`); most of `test_tenacity.py` uses `wait=none`
  or mocked clocks.
- All three derive their version from git metadata at build time. The sandbox
  image has a fresh single-commit repository, which is enough for setuptools-scm /
  hatch-vcs / setuptools-git-versioning to produce a placeholder version. If an
  install ever fails on "no version", use `install = "git tag v0.0.0 && pip install -e ."`.

## A task source directory

```
evals/benchmark/sources/<id>/
  task.toml         id, repo, commit, category, difficulty, test_command, [env]
  bug.patch         clean tree -> buggy tree  (the mutation; source files only)
  hidden.patch      adds tests/test_repopilot_<id>.py (new files only)
  description.md    the bug report the agent sees
```

`make_task.py` turns that into `evals/benchmark/tasks/<id>.json` with the derived
`gold_patch` (exact reverse of the mutation), `gold_files` / `gold_symbols` (from
the hunks and the AST of the buggy files), and `fail_to_pass` / `pass_to_pass`
(from two sandbox runs: buggy + hidden tests versus fixed + hidden tests). It
refuses to write a task whose hidden tests pass with the bug, whose gold patch
breaks an existing test, whose hidden patch edits an existing file, or whose
test command never collects the hidden tests.

## Workflow for one task

```bash
# 0. one-time: a scratch clone per repo (work/ is git-ignored)
git clone https://github.com/tkem/cachetools work/cachetools
cd work/cachetools && git checkout 4500e3d04288738d25acbb4973eb3c3e1bf41db9 && cd ../..

# 1. template
uv run python scripts/make_task.py --init evals/benchmark/sources/cachetools_001 \
    --id cachetools_001 --repo https://github.com/tkem/cachetools \
    --commit 4500e3d04288738d25acbb4973eb3c3e1bf41db9 \
    --test-command "pytest tests/test_lru.py tests/test_ttl.py" --category cache_invalidation

# 2. inject the bug in the scratch clone, export it, restore the clone
#    (every git command in steps 2-3 runs INSIDE work/cachetools -- `git diff` in the
#    RepoPilot root would export RepoPilot's own uncommitted changes instead)
cd work/cachetools
#    ... edit src/cachetools/__init__.py in your editor ...
git diff > ../../evals/benchmark/sources/cachetools_001/bug.patch
head -3 ../../evals/benchmark/sources/cachetools_001/bug.patch   # must name src/cachetools/...
git checkout -- .

# 3. write the hidden test as a NEW file, export it, restore the clone
#    ... create tests/test_repopilot_cachetools_001.py in your editor ...
git add -N tests/test_repopilot_cachetools_001.py
git diff > ../../evals/benchmark/sources/cachetools_001/hidden.patch
head -3 ../../evals/benchmark/sources/cachetools_001/hidden.patch  # must name the new test file
git reset -q && rm tests/test_repopilot_cachetools_001.py
cd ../..

# 4. write description.md, then derive + validate (needs Docker)
uv run python scripts/make_task.py evals/benchmark/sources/cachetools_001 --dry-run
uv run python scripts/make_task.py evals/benchmark/sources/cachetools_001
uv run python scripts/validate_tasks.py
uv run python -m evals.runner --solver gold --expect pass --ids cachetools_001 --repeat 2
```

Always produce patches with `git diff`. `git apply` anchors hunks with fewer than
three context lines to the start or end of the file, so hand-written short hunks
fail to apply for no obvious reason.

## Rules

**Mutations.** One realistic mistake, 1–5 lines, in source files only (test files
are rejected). The buggy file must still parse. Prefer faults a maintainer could
plausibly ship: an off-by-one in a bound, an inverted or incomplete condition, a
missing `None`/empty check, a forgotten reset or copy, a retry that gives up one
attempt early, a cache entry that survives its expiry, an exception swallowed or
caught too broadly. Never a syntax error, never a change that only affects a
message string.

**Descriptions.** Write what a user would observe and how to reproduce it; never
name the fix. Difficulty is set by how much the description gives away:

- *easy* — names the function or the test that fails
  ("`LRUCache.__getitem__` no longer refreshes the entry; `test_lru` fails").
- *medium* — describes behaviour through the public API only
  ("after `cache[k]` is read, `k` is still the first entry evicted").
- *hard* — the symptom is one or two layers away from the cause
  ("a `@cached` function recomputes on every call once the cache has filled up").

**Hidden tests.** One new file named `tests/test_repopilot_<id>.py` (or the repo's
test directory), exercising the behaviour with inputs different from the existing
tests. It must fail with the bug and pass with the fix — `make_task` checks both.
Whether the *existing* tests also catch the bug is your choice: most tasks should
have visible failing tests (the agent can run them and iterate), and three to five
tasks should be caught only by the hidden test (the agent must reproduce from the
description alone). The report tells you which kind you made.

**Description and hidden tests must agree.** Every behaviour a hidden test checks
must be stated in the description or follow directly from it. Otherwise a solver
that fixes exactly what was reported can be failed for a side effect it was never
told about — the "tests check functionality not mentioned in the problem
statement" failure that SWE-bench Verified had to filter out. When you shorten a
description to raise the difficulty, drop the matching test too. A useful check:
imagine a *different* reasonable fix for the described symptom (a guard at the
point where the symptom appears rather than at the root cause) and make sure it
would pass the hidden tests as well; if it would not, either describe the extra
behaviour or remove the test.

**Test command.** A pytest command run from the repo root, targeting the one or
two test files that cover the mutated code, so a run stays under ~10 s (the
oracle validation runs every task several times). Do not list the hidden file;
the harness appends it at evaluation. `pass_to_pass` is derived from exactly
what this command collects.

**Ids and provenance.** `<repo>_<nnn>`, three digits. Keep the source directory
in git next to the generated JSON — the JSON is the artifact, the directory is the
audit trail.

## Coverage (v0: 14 tasks, all built and validated 2026-09-10)

| id | repo | category | difficulty | mutation site |
| --- | --- | --- | --- | --- |
| cachetools_001 | cachetools | cache_invalidation | medium | `TTLCache` expiry check |
| cachetools_002 | cachetools | cache_invalidation | hard | `TLRUCache` / `_TimedCache.expire` |
| cachetools_003 | cachetools | state_management | medium | `LRUCache.__getitem__` touch order |
| cachetools_004 | cachetools | off_by_one | easy | `Cache.__setitem__` size accounting |
| cachetools_005 | cachetools | wrong_condition | medium | `LFUCache.popitem` / `cached()` key handling |
| tenacity_001 | tenacity | retry_logic | easy | `stop_after_attempt` boundary |
| tenacity_002 | tenacity | retry_logic | medium | `wait_exponential` growth / cap |
| tenacity_003 | tenacity | exception_handling | medium | `retry_if_exception_type` matching |
| tenacity_004 | tenacity | exception_handling | hard | `Retrying.iter` reraise path |
| tenacity_005 | tenacity | missing_check | medium | `stop_after_delay` / `wait_chain` bounds |
| toolz_001 | toolz | off_by_one | easy | `itertoolz.sliding_window` window offset |
| toolz_002 | toolz | wrong_condition | medium | `itertoolz.unique` key bookkeeping |
| toolz_003 | toolz | missing_check | medium | `dicttoolz.dissoc` missing-key guard |
| toolz_004 | toolz | state_management | hard | `functoolz.memoize` default cache |

The constraints that mattered: every category at least once (`other` is
deliberately unused), roughly 3 easy / 8 medium / 3 hard, and each repository
contributing tasks from more than one category. Only two of the fourteen tasks
(`toolz_003`, `toolz_004`) are caught by the hidden test alone, short of the
three-to-five target above — the Phase 4 expansion should favour hidden-only
tasks, which measure reproduction from the description rather than from a red
test. `tests/test_benchmark_tasks.py` keeps every shipped task consistent with
its source directory; when a task is regenerated, commit both.

## Acceptance (Phase 0 exit)

```bash
uv run python -m evals.runner --solver null --expect fail
uv run python -m evals.runner --solver gold --expect pass --repeat 2
```

Both must exit 0 on the full `evals/benchmark/tasks/` directory: every task fails
untouched, passes with its reference fix, and gives identical per-test outcomes
on repeated runs. Record the resulting `summary.json` numbers in the README
checklist — they describe the harness, not the agent. v0 passed on 2026-09-10:
`null` 0/14 (all `fail_to_pass_failing`, 21 s), `gold` 28/28 with 14/14 tasks
deterministic across two repeats (38 s); the README's benchmark section keeps
the numbers.
