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
  task.toml         id, repo, commit, category, test_command, suite, report_level,
                    [entry_points], [authored_difficulty], [site_note], [env];
                    real tasks: source = "real", fix_commit
  bug.patch         clean tree -> buggy tree  (the mutation; source files only; mutation tasks)
  hidden.patch      adds tests/test_repopilot_<id>.py (new files only)
  description.md    the bug report the agent sees
```

`make_task.py` turns that into `evals/benchmark/tasks/<id>.json` with the derived
`gold_patch` (exact reverse of the mutation, or a real task's fix hunks),
`gold_files` / `gold_symbols` (from the hunks and the AST of the buggy files),
`fail_to_pass` / `pass_to_pass` (from two sandbox runs: buggy + hidden tests
versus fixed + hidden tests), and — since Bench v1 — `surface_symbols` /
`surface_files` / `cross_module` from the report audit and the derived
`difficulty` (see "Bench v1" below). It refuses to write a task whose hidden
tests pass with the bug, whose gold patch breaks an existing test, whose hidden
patch edits an existing file, whose test command never collects the hidden
tests, or whose report names what its tier forbids.

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
name the fix. In v0 the authored difficulty was set by how much the description
gives away (easy names the function or the failing test; medium describes
behaviour through the public API; hard puts the symptom a layer or two from the
cause). Since Bench v1 that judgement is a rule: the report's *tier*
(`report_level`) is declared and audited, and `difficulty` is derived (below).
The authored label survives only as `authored_difficulty` in `task.toml`.

**Hidden tests.** One new file named `tests/test_repopilot_<id>.py` (or the repo's
test directory), exercising the behaviour with inputs different from the existing
tests. It must fail with the bug and pass with the fix — `make_task` checks both.
A hidden test that passes with the bug too is refused unless `task.toml` says
`hidden_pass_to_pass = true` (a regression guard transplanted with a real fix,
see below); it then ships as `pass_to_pass`, the task JSON records the flag,
`tests/test_benchmark_tasks.py` checks that guards and flag agree, and at least
one hidden test must still be fail-to-pass.
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

| id | repo | category | difficulty (derived; authored) | mutation site |
| --- | --- | --- | --- | --- |
| cachetools_001 | cachetools | cache_invalidation | easy (medium) | `TTLCache` expiry check |
| cachetools_002 | cachetools | cache_invalidation | easy (hard) | `TLRUCache` / `_TimedCache.expire` |
| cachetools_003 | cachetools | state_management | easy (medium) | `LRUCache.__getitem__` touch order |
| cachetools_004 | cachetools | off_by_one | easy (easy) | `Cache.__setitem__` size accounting |
| cachetools_005 | cachetools | wrong_condition | medium (medium): cross-module | `LFUCache.popitem` / `cached()` key handling |
| tenacity_001 | tenacity | retry_logic | easy (easy) | `stop_after_attempt` boundary |
| tenacity_002 | tenacity | retry_logic | easy (medium) | `wait_exponential` growth / cap |
| tenacity_003 | tenacity | exception_handling | easy (medium) | `retry_if_exception_type` matching |
| tenacity_004 | tenacity | exception_handling | easy (hard) | `Retrying.iter` reraise path |
| tenacity_005 | tenacity | missing_check | easy (medium) | `stop_after_delay` / `wait_chain` bounds |
| toolz_001 | toolz | off_by_one | easy (easy) | `itertoolz.sliding_window` window offset |
| toolz_002 | toolz | wrong_condition | easy (medium) | `itertoolz.unique` key bookkeeping |
| toolz_003 | toolz | missing_check | medium (medium): hidden-only | `dicttoolz.dissoc` missing-key guard |
| toolz_004 | toolz | state_management | hard (hard): hidden-only, two hunks | `functoolz.memoize` default cache |

The constraints that mattered: every category at least once (`other` is
deliberately unused), roughly 3 easy / 8 medium / 3 hard as authored, and each
repository contributing tasks from more than one category. Only two of the
fourteen tasks (`toolz_003`, `toolz_004`) are caught by the hidden test alone,
short of the three-to-five target above — which is why Bench v1 (below) makes
hidden-only tasks half of what it adds. Under the v1 rule the authored labels
mostly collapse to *easy*: every v0 report names the changed symbol, and that
is what the leak audit found. `tests/test_benchmark_tasks.py` keeps every
shipped task consistent with its source directory and its difficulty equal to
the derived one; when a task is regenerated, commit both.

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

## Bench v1 (2026-09-22): what changed in the workflow

The design and the pre-registered expectations are in
`docs/bench-v1-design.md`; this is the authoring side of it. Everything below
is enforced by `make_task.py` unless it says "printed".

**Repositories.** click 8.3.0, rich 14.1.0, jinja 3.1.6, sqlparse 0.5.3, pinned
in the design's §3 with their install commands and test layouts; the survey
notes there (which test files cover which module, what never goes in a test
command) are the starting point for every task.

**New `task.toml` keys.** `suite = "v1"`; `report_level` (`public_api` by
default, `symptom_only` for the strict tier, `internal` only for v0);
`entry_points` (symptom_only: the at most three names the report may use, e.g.
`["sqlparse.format"]` or `["Console.print", "Panel"]`); `authored_difficulty`
(your estimate, kept for the record, never shipped); `site_note` (one line on
why a site the site audit flagged was kept); for a real task `source = "real"`
and `fix_commit`, with `commit` = the fix's parent and no `bug.patch`.
`difficulty` is no longer a key: the shipped value is derived — one point each
for cross-module, hidden-only, a multi-site or cross-file fix and a
symptom-only report; 0 easy, 1 medium, 2+ hard.

**The report audit** runs before the image build and fails the task on: the
name of a changed symbol (bare or qualified, whole identifier, case-insensitive;
a public alias such as the `truncate` filter for `do_truncate` is fine and only
warned about), the name or path of a changed file, a traceback frame or
`file.py:line` reference, a private name (leading underscore in the name or in
a module path component) at the `public_api` tier, anything that resolves to a
repository symbol and is not an entry point at the `symptom_only` tier. It
resolves the report's identifiers against the buggy tree's symbol table — the
one `search_symbol` uses — so back-tick every identifier: in prose only dotted
names, snake_case, CamelCase and calls are recognised, a bare `Table` in a
sentence is English. What it resolves becomes `surface_symbols` /
`surface_files`, and `cross_module` is "no surface file is a gold file"
(defining files, not re-exporting ones: cachetools' `cached` is defined in
`__init__.py`, so `cachetools_005` is cross-module against `_cached.py`).

**The site audit** (printed by `--dry-run` and by every build) lists comments
and docstrings within eight lines of a hunk that share two or more sub-tokens
with the changed lines, changelog lines at the pinned commit that mention a gold
symbol, and buggy lines matching a banned textbook shape (mutable default
argument, `if not x` where a `None` check belongs, `type(x) ==`, an unguarded
`del d[k]`, a bare `except:`, `== None`). Read it and move the site, or keep it
with a `site_note`.

**Real tasks.** `make_task --init SRC --source real --fix-commit <sha> --commit
<parent-sha> ...`; `make_task` derives the gold patch as the fix commit's
Python source hunks against its parent (tests, changelog and docs excluded),
checks the parent relationship, and expects the fix's tests transplanted into a
new `tests/test_repopilot_<id>.py` in `hidden.patch` like any other task. Upstream
tests usually mix the cases that show the bug with guards that pass either way
(`click_009` carries six of them); `hidden_pass_to_pass = true` lets the guards
through as `pass_to_pass`, where they still catch a fix that breaks them. The
report is written by us at the public_api or symptom_only tier from what the
upstream issue reports — never the issue text.

**Where the work happens.** The cloud workspace can clone the four repositories
and run their visible test suites, so a task's `bug.patch`, `hidden.patch` and
description arrive on the Mac already checked: the mutation applies, the
visible suite behaves as claimed (green for a hidden-only task), the hidden
test fails with the bug and passes without it, the audit passes. On the Mac:
`uv run python scripts/make_task.py evals/benchmark/sources/<id>` (Docker),
read the report, commit the source directory and the JSON together. One gotcha
of the scratch-clone workflow: run the repository's suite with
`PYTHONDONTWRITEBYTECODE=1`, because a mutation that keeps a line's byte length
(`return idx, not item.is_eager` for `return not item.is_eager, idx`) and is
reverted within the same second leaves a `.pyc` that Python still trusts, and
the clean tree then fails the mutated tests. `make_task` is immune (fresh
exports in a fresh container). The sandbox pins `pytest==9.0.3`
(`docker/base.Dockerfile`; issues.md #13); run the cloud pre-check with the same
version, because a repository that tests with `filterwarnings = ["error"]`
turns a newer pytest's deprecation warnings into collection errors.

**Refreshing metadata.** `make_task SRC --refresh` re-runs only the git half —
patch normalization, the audit, the derived fields — on an already-built task,
keeping the sandbox-derived test lists and patch text. This is how the 14 v0
files were regenerated on 2026-09-22 (no patch, test, command or description
changed). It refuses when the fix, the hidden tests, the test command or the
description differ from the shipped task; those need a full `make_task`.

**Checking the suite.** `uv run python scripts/validate_tasks.py` prints one
line per task with the v1 fields and the suite report: counts for v0, v1-new
and all, and the v1-new targets of the design's §2 (36 tasks, hidden-only ≥ 18,
cross-module ≥ 18, symptom_only 12, multi-site ≥ 8 with cross-file ≥ 4, every
category ≥ 2, at most 15 changed lines). `--audit` re-runs the report audit of
every task against its tree; `--strict` exits 1 on a missed target or an
unaudited task.

**Running the ablation.** `python -m evals.runner --solver baseline --suite
v1-new --report {full,redacted,generic} [--no-run-tests]` gives the four arms
of `docs/leak-audit.md` §4 (`--report redacted` is arm B′: every repository
symbol in the report replaced by a placeholder, derived at run time from the
task file); `scripts/memorization_probe.py --models ...` asks each model to
write the gold symbols from memory and scores the replies against the pinned
source.

## click (v1, 9 tasks, built 2026-09-22)

Pinned commit `00fadb89` (8.3.0) for the mutations; the real task sits at the
parent of its fix. 1,285 tests in 2.2 s, no third-party dependencies. Test
files map cleanly onto modules: `test_options.py`, `test_arguments.py`,
`test_basic.py` and `test_commands.py` exercise `core.py` with `parser.py`
underneath; `test_types.py` covers `types.py`; `test_formatting.py` covers
`formatting.py` and `_textwrap.py`; `test_termui.py` covers `termui.py` and
`_termui_impl.py` (prompt, progress bar; a few tests skip off Windows);
`test_normalization.py` is the only place `token_normalize_func` is exercised.
Everything is reachable through `@click.command`/`@click.option` and
`CliRunner`, so every report is a CLI transcript.

| id | site | fault | category | report | hidden-only | cross-module | shape |
|---|---|---|---|---|---|---|---|
| click_001 | `parser.py` `_OptionParser.add_option` | option names not normalized when declared, only when matched | propagation | public_api | yes | yes | single_line |
| click_002 | `parser.py` `_OptionParser._get_value_from_state` | a lone `-` taken for an option (the `len > 1` guard dropped), so an optional-value option rejects `-` | missing_check | symptom_only (`click.command`, `click.option`, `click.File`) | yes | yes | single_line |
| click_003 | `types.py` `_NumberRangeBase.convert` + `IntRange._clamp` | open bounds treated as closed when checking and when clamping | off_by_one | public_api | no (6 visible) | no | multi_site |
| click_004 | `types.py` `Choice.convert` | choices mapped without the context's normalization function; the typed value is normalized | propagation | public_api | yes | no | single_line |
| click_005 | `formatting.py` `HelpFormatter.write_dl` | the section indent not subtracted from the description width: option help runs past the terminal | off_by_one | public_api | yes | yes | single_line |
| click_006 | `core.py` `Option.prompt_for_value` | a callable default not evaluated before prompting | wrong_condition | public_api | yes | yes | single_line |
| click_007 | `core.py` `iter_params_for_processing` | command-line order sorted before eagerness: `--help`/`--version` lose to an earlier invalid option | ordering | symptom_only (`click.command`, `click.option`, `click.version_option`) | no (11 visible) | yes | single_line |
| click_008 | `_textwrap.py` `TextWrapper.extra_indent` | the subsequent indent not restored after an indented paragraph | state_management | symptom_only (`click.command`, `click.wrap_text`) | yes | yes | single_line |
| click_009 | `core.py` `Option.__init__` — real fix `4fd2fea0db` (#2930, issues #2894/#2897), base `9ce34f20` | a flag option with an explicit `type` gets no flag value | wrong_condition | public_api | yes | yes | multi_line |

Against the design's planned areas (§4.3): area 1 (option matching) and area 5
(help formatting) came out hidden-only rather than visible — the suite never
declares an option with capitals under normalization and never wraps help text
at the exact boundary; area 3 (ranges) is multi-site as planned but visible,
because `test_types.py::test_range*` pins every boundary of `convert` and
`_clamp`, so no hidden-only site exists there; area 2 (`_unpack_args`) was
replaced by the lone-`-` guard in the same parser module — the `_unpack_args`
sites are either pinned by `test_arguments.py` or sit under comments that state
the invariant; area 6 (defaults / `show_default`) moved to the prompt path of
the same feature, since every `show_default` branch is pinned by
`test_options.py`; area 8 (`utils.py`) was replaced by the wrapping helper,
which gave the set its state-management task. Flags for the nine: hidden-only
7, cross-module 7, symptom_only 3, multi-site 1 (no cross-file); every category
except `exception_handling` and `cache_invalidation` appears. Under the derived
rule the set is 7 hard / 2 medium / 0 easy (v0: 11 / 2 / 1 the other way round),
which is what "cross-module and hidden-only both score a point" produces; the
leak ablation is what tells whether that label means anything.

Every task was pre-checked in the cloud before delivery (mutation applies,
visible suite as claimed, hidden tests fail-to-pass, audit ok, real-task
guards allowed); the Docker derivation on the Mac is the shipped record.
