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
sentence is English. In code (fences and back-ticks) the contents of string
literals, keyword-argument names (`Console(width=80)`) and the module path of
an import are data, not references; an import of a *private* changed module
(`from rich import _wrap`) is a violation, of a public one a same-module
warning. At `symptom_only`, naming a class as an entry point covers its
methods (`Table` allows `table.add_row`). Symbols under `examples/`, `docs/`,
`scripts/`, `tools/`, `benchmarks/` and test paths never resolve. What it
resolves becomes `surface_symbols` / `surface_files`, and `cross_module` is
"no surface file is a gold file" (defining files, not re-exporting ones:
cachetools' `cached` is defined in `__init__.py`, so `cachetools_005` is
cross-module against `_cached.py`).

One consequence of the audit that shapes site selection: the report may not
name a changed symbol, so a site whose natural reproduction *is* the changed
method (`Text.expand_tabs`, `Text.append`, `Table.add_row`) cannot be shipped
at any v1 tier, however good the bug — rich's first real-fix candidate
(`Text.append` looping on itself) was dropped for exactly this reason, and
the mutation in `Text.expand_tabs` moved to `Text.right_crop`, which users
reach through `set_length` and `remove_suffix`.

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
turns a newer pytest's deprecation warnings into collection errors. Node ids
are always repository-relative: the sandbox plugin rewrites pytest's
rootdir-relative ids (issues.md #14 — rich keeps `tests/pytest.ini`, which
moves the rootdir into `tests/`), and a cloud pre-check must pass
`--rootdir=<repo>` for the same reason.

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

## rich (v1, 9 tasks, built 2026-09-25)

Pinned commit `2dca1b70` (14.1.0) for the mutations; the real task sits at the
parent of its fix (14.0.0 + one commit). 856 tests in 6 s (24 skipped), needing
pygments and markdown-it-py. Tests render into a `Console(file=io.StringIO(),
width=N, force_terminal=…, color_system=…, legacy_windows=False)` and compare
strings; every hidden test here fixes all four. `tests/test_text.py` covers
`text.py` and, through `Text.wrap`, `_wrap.py` and `cells.py` (those two are
pinned tightly — every boundary mutation tried there was visible);
`tests/test_table.py` covers `table.py` and reaches `_ratio.py`;
`tests/test_style.py`, `test_markup.py`, `test_panel.py`, `test_padding.py`,
`test_pretty.py`, `test_ansi.py`, `test_layout.py` map one to one. Two
upstream facts to know before choosing a site: `Style` caches its ANSI codes
without the colour system (`Style.parse("#ff8800")` rendered on a truecolor
console keeps truecolor codes on a standard one), so nothing near
`Style.render` is a clean site; and `tests/pytest.ini` moves the pytest
rootdir (issues.md #14).

| id | site | fault | category | report | hidden-only | cross-module | shape |
|---|---|---|---|---|---|---|---|
| rich_001 | `text.py` `Text.right_crop` | the cached length not reduced when characters are cropped: a second `set_length` cuts instead of padding | cache_invalidation | public_api | yes (`test_card_render` also fails with the bug, but it fails in the sandbox with the fix too, so it is kept out of the command) | no | single_line |
| rich_002 | `style.py` `Style._add` | link precedence inverted: the first link wins over one applied later (nested markup links, `stylize` twice) | ordering | symptom_only (`Console.print`, `Text.stylize`) | yes | yes | multi_line |
| rich_003 | `padding.py` `Padding.__rich_console__` | bottom padding not subtracted from a fixed height: a panel with `height=` loses its bottom padding line | off_by_one | symptom_only (`Panel`, `Console.print`) | yes | yes | single_line |
| rich_004 | `markup.py` `render` | closing tags not normalized: `[bold red]…[/red bold]` and `[/BOLD]` raise `MarkupError` | wrong_condition | public_api | no (2 visible) | yes | single_line |
| rich_005 | `_ratio.py` `ratio_distribute` | the minimum floor dropped: an expanded table's ratio-1 column shrinks below its padding and its content vanishes | missing_check | symptom_only (`Table`, `Console.print`) | yes (the one failing visible test, `test_tools`, is outside the command) | yes | single_line |
| rich_006 | `table.py` `Table._calculate_column_widths` + `Table._render` | `no_wrap` read at neither site: the column is squeezed and its text wraps | propagation | public_api | no (1 visible) | no | multi_site |
| rich_007 | `style.py` `Style.update_link` | the cached hash copied from the source style: a linked style compares equal to the unlinked one, so `Text.from_ansi` links run on and `export_html` drops the href | cache_invalidation | public_api | no (1 visible, `test_ansi`) | yes | single_line |
| rich_008 | `pretty.py` `traverse._traverse` | a container's id never popped from the visited set: a repeated (not cyclic) object prints as `...` | state_management | public_api | no (1 visible; `tests/test_pretty.py` imports `attr`, so the task installs `attrs`) | yes | multi_line |
| rich_009 | `panel.py` `Panel.__rich_console__` — real fix `30e5ed61` (#3569), base `69e1618f` | the panel style not applied to title and subtitle (background missing) | propagation | public_api | yes | no | multi_site (3 hunks) |

Against the plan (§4.3): area 1 (cells) and area 3 (`_wrap.divide_line`) were
dropped — `test_cells.py` and the `test_wrap_*` cases pin every boundary, and
the one hidden-only mutation found there was a no-op; area 2 (segment
cropping) had two hidden-only sites but both only show with control segments,
so it was replaced by the ANSI-decoding hash bug (rich_007); area 4 (ratio)
moved from `ratio_resolve`, whose only clean mutation sits under a comment that
states the fix ("we need to add the remainder to the following line"), to
`ratio_distribute`; area 6 (markup) is as planned; area 7 (style combination)
is the link precedence; area 8 (align/padding) is the padding height; the
pretty-printer's visited set (rich_008) replaced the planned column-collapse
propagation, which became the `no_wrap` two-site task (rich_006). The real fix
is `30e5ed61` (panel title background, three hunks) rather than the planned
`f2ee29531b` (`Text.append` looping on itself), whose report would have had to
name the changed method. Flags for the nine: hidden-only 5, cross-module 6,
symptom_only 3, multi-site 2 (no cross-file); categories cache_invalidation 2,
propagation 2, ordering, off_by_one, wrong_condition, missing_check,
state_management 1 each. Derived difficulty 5 hard / 4 medium (rich_001 counts as
hidden-only once `test_card.py` is out of its command).

Two things the Docker derivation showed that the cloud pre-check could not: a
test file's third-party import (`attrs`) is a dev dependency the task image does
not have unless `env.install` says so, and `tests/test_card.py::test_card_render`
renders differently inside the sandbox (it fails there with and without the
fix; the harness excludes such tests, but a permanently red visible test is a red
herring for the agent, so it was dropped from rich_001's command).

## jinja (v1, 9 tasks, built 2026-09-25)

Pinned commit `15206881` (3.1.6, src layout) for the mutations; the real task
sits at the parent of its fix (3.0.1 + 8 commits). 784 tests in 2 s with
`tests/test_async.py` and `tests/test_async_filters.py` left out (they import
`trio`); test commands never include those two, and a hidden test that needs the
async path drives it with `asyncio.run(template.render_async(...))` on an
`Environment(enable_async=True)`. Templates are mostly built with
`Environment().from_string(...)`; loader tests use `DictLoader` and `tmp_path`
(`tests/conftest.py` offers `env`, `dict_loader`, `filesystem_loader`,
`package_loader`, `choice_loader`, `prefix_loader`). Two upstream facts that
shaped the sites: constant folding (`nodes.py`, `as_const`) evaluates any filter
or test whose arguments are all literals at compile time, so a symptom
described with literal arguments may never reach the code path meant (use a
variable); and the `with context` include/import path forwards the *local*
variables of the enclosing frame separately from `context.get_all()`, which is
what makes loop and `{% with %}` variables visible inside includes.

| id | site | fault | category | report | hidden-only | cross-module | shape |
|---|---|---|---|---|---|---|---|
| jinja_001 | `environment.py` `Environment.getattr` + `sandbox.py` `SandboxedEnvironment.getattr` | `except AttributeError` widened to `except Exception` in both: a property that raises falls through to the item lookup / undefined | exception_handling | symptom_only (`Environment`, `SandboxedEnvironment`, `Template.render`) | yes | no | cross_file (2 files) |
| jinja_002 | `nodes.py` `_FilterTestCommon.as_const` | `except Exception` narrowed to `(TypeError, ValueError)`: a custom filter/test raising anything else on literal arguments fails at load time, even in a dead branch | exception_handling | symptom_only (`Environment`, `Template.render`) | yes | yes | single_line |
| jinja_003 | `utils.py` `LRUCache.__setitem__` | the existing-key branch dropped: re-setting a cached key (a reload with `auto_reload`) duplicates its queue entry and evicts a neighbour; later evictions raise `KeyError` out of `get_template` | cache_invalidation | public_api | yes | yes | multi_line |
| jinja_004 | `parser.py` `Parser.parse_call_args` | the `not kwargs` guard dropped: `f(b=1, 2)` compiles and calls `f(2, b=1)` instead of raising `TemplateSyntaxError` | missing_check | symptom_only (`Environment`, `Template.render`, `TemplateSyntaxError`) | yes | yes | single_line |
| jinja_005 | `compiler.py` `CodeGenerator.visit_ScopedEvalContextModifier` | the runtime `context.eval_ctx.revert(...)` no longer emitted after a scoped `{% autoescape %}` block (the compile-time revert stays): `pass_eval_context` filters/functions and `join` see the block's setting for the rest of the template | state_management | public_api | yes | yes | single_line |
| jinja_006 | `runtime.py` `LoopContext.length` + `AsyncLoopContext.length` | the peeked item not counted when a generator's size is computed after `loop.last`/`loop.nextitem`: `loop.revindex` ends at 0, `revindex0` at -1 | off_by_one | public_api | yes | no | multi_site (2 hunks) |
| jinja_007 | `loaders.py` `FileSystemLoader.get_source` (`uptodate` closure) | `except OSError: return False` dropped: a deleted template raises `FileNotFoundError` from `get_template` instead of `TemplateNotFound`, so `ChoiceLoader` and `select_template` fallbacks never run | exception_handling | public_api | yes | no | multi_line |
| jinja_008 | `compiler.py` `CodeGenerator.visit_Include` + `CodeGenerator._import_common` | the local-context dump dropped from both `with context` paths: includes and context imports do not see loop / `{% with %}` / block-local variables | propagation | public_api | no (3 visible, all on the include side; the import side is hidden-only) | yes | multi_site (2 hunks) |
| jinja_009 | `compiler.py` `CodeGenerator.pull_dependencies` + `idtracking.py` `Symbols.dump_stores` — real fix `4c703ec` (#1452/#1453), base `02071b3e` | filter/test names and stored names iterated from sets: `compile_templates` output changes with the hash seed | ordering | public_api | yes | yes | cross_file (2 files) |

Against the plan (§4.3): area 1 (whitespace control) and area 5 (grouping
filters) were not used — the string-filter sweep found only `truncate`'s
leeway boundary and two `wordwrap` flags hidden-only, none of them worth a
slot; area 2 (argument parsing) became the positional-after-keyword guard in
`parse_call_args` (the `parse_signature` default-order guard is pinned by
`test_arguments_defaults_nonsense`); area 3 (loop context) is the generator
length peek, doubled onto the async context; area 4 (string filters) gave way
to the scoped-autoescape runtime revert (compiler); area 6 (idtracking) is
covered by the real fix instead; area 7 (undefined handling) became the
attribute-lookup exception boundary, which the sandbox duplicates — the first
cross-file mutation in the suite; area 8 (`LRUCache`) is as planned. The real
fix is `4c703ec` (deterministic `compile_templates`, two files) rather than the
planned `051df10c7b` (`required` block check, one file): the two-file fix is
worth more to the cross-file target and its upstream test transplants without
guards. Flags for the nine: hidden-only 8, cross-module 6, symptom_only 3,
multi-site 4 (cross-file 2); categories exception_handling 3, and one each of
cache_invalidation, missing_check, state_management, off_by_one, propagation,
ordering. Derived difficulty 8 hard / 1 medium (jinja_007 is the medium one:
same-module surface, single site).

Sites rejected on the way, with the reason, so they are not tried again:

- `Environment.overlay` sharing the parent's cache object (hidden-only, a clean
  cache_invalidation) — the natural report cannot avoid the word `overlay`,
  which is the changed method (the rich_001 lesson again; the audit bans the
  gold symbol's last component as a whole identifier, prose included).
- `LoopContext.length` reports — same rule: the report for jinja_006 never says
  `length`; it describes `loop.revindex` / `revindex0` and "the size the loop
  reports", which is a legitimate way a user would see it.
- `runtime.new_context` dropping the `missing` filter for locals and
  `Context.derived` dropping `eval_ctx` — both hidden-only, both observable, kept
  as backups (the first is contrived to reproduce, the second overlaps jinja_005).
- `Environment.getitem` / `getattr` narrowed excepts, `_load_template` without
  `is_up_to_date`, `visit_Include` without locals alone, `Compare.as_const`
  chain, `parse_signature`, `do_truncate` killwords, `do_indent` blank lines —
  all visible (2–18 tests); `debug.get_template_locals` depth `<=` and the
  `bccache` checksum comparison — not observable / under a comment stating the
  reason.

Generic changelog lines exist for two of the areas ("Fix behavior of `loop`
control variables such as `length` … when looping over a generator", 2.11;
"Inclusions and imports 'with context' forward all variables now", 2.1). They
name the area, not the fix, and predate the pinned code by years; the site
audit prints them and they were accepted.
