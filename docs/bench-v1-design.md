# RepoPilot-Bench v1 — design and pre-registration

Written 2026-09-19, before any v1 task is built. Design and pre-registered
experiment, as for Phases 2 and 3 (`docs/runtime-design.md`,
`docs/retrieval-design.md`): the expectations are written down first so the
results can be read against them.

Marco's decisions (2026-09-19): new repositories **click, rich, jinja,
sqlparse**; **50 tasks = v0's 14 + 36 new**; the new tasks are controlled
mutations plus **one real historical fix per repository**; the leak ablation of
`docs/leak-audit.md` §4 is run as v1's acceptance experiment.

## 1. What failure mode, what metric (spec §19)

The failure mode is the benchmark's, not the agent's. The leak audit
(`docs/leak-audit.md`) established that v0 cannot separate configurations on
success: every model localizes every task, through five channels —
identifiers in the report, visible fail-to-pass tests, self-describing
mutation sites, textbook anti-patterns, memorized libraries. Every Phase 2 and
Phase 3 result is therefore a result about *efficiency* (steps, tokens, cost),
and the spec's headline metric — task success, and what retrieval and the
runtime do to it — has never had room to move. v1 exists to make it move.

| leak channel (audit §3) | v1 lever | metric that must move |
|---|---|---|
| 1. identifiers in the report | reports written at the *public API* or *symptom* tier, audited mechanically (§6) | leak ablation: A − B′ (full vs redacted report) ≤ 10 pp on v1 (v0: ≥ 20 pp) |
| 2. visible fail-to-pass tests | half of the new tasks are *hidden-only* (the suite stays green with the bug) | success on hidden-only tasks below visible-failure tasks by ≥ 15 pp |
| 3. self-describing sites | site rules (§4.1): no comment, docstring or release note that states the behaviour within reach of the hunk | leak ablation: A − D (report vs "there is a bug") ≥ 30 pp on v1-new (v0: ≤ 20 pp) |
| 4. textbook anti-patterns | mutation catalogue (§4.2) of domain-logic faults; a banned-shapes list | first `edit_file` in a gold file 65–85 % on v1-new (v0: 100 %) |
| 5. memorized libraries | a memorization probe per gold symbol (§9.3), reported alongside; real fixes from 2023–2025 | recalled symbols reported; D-arm success conditioned on recall |
| (size) small, flat repositories | rich 23.5k lines (v0's largest: toolz 3.0k), jinja 11.7k, click 8.9k; cross-module symptoms by construction | offline Recall@10 (file) 0.55–0.80 on v1-new (v0: 0.93–1.00); Haiku `budget_tokens` 10–30 % |

What v1 must keep from v0: deterministic verdicts (null 0 / N, gold N / N
twice), no LLM judge, every field derived by running the code, the §9.1 budget
unchanged for every configuration.

## 2. Shape

- **50 tasks**: the 14 v0 tasks (`suite: v0`, unchanged patches and tests,
  regenerated with the v1 derivations of §7) plus 36 new (`suite: v1`):
  four repositories × (8 controlled mutations + 1 real fix). Results are
  reported for v1 (all 50), for v1-new (36), and broken down by suite,
  repository, `hidden_only`, `cross_module` and `report_level`.
- **Budget**: spec §9.1 as before — 30 steps, 40 tool calls, 5 test runs,
  100k tokens, $0.50, 600 s. Larger repositories make the token cap bind more
  often on Haiku; that is a measured outcome, not a knob.
- **Structural targets for the 36 new tasks** (checked by `validate_tasks.py`,
  reported if missed):

| property | target | why |
|---|---|---|
| hidden-only (no visible fail-to-pass test) | ≥ 18 | leak 2; v0 has 2 of 14 |
| cross-module (surface files ∩ gold files = ∅, §7) | ≥ 18 | retrieval must find a file the report does not point at |
| `report_level: symptom_only` | 12 (3 per repo) | leak 1, the strict tier |
| multi-site (≥ 2 hunks) | ≥ 8, of which cross-file ≥ 4 | localization of one site does not finish the task |
| every category | ≥ 2 tasks | failure breakdown by category (spec §11.2) |
| mutation size | 1–15 changed source lines per task | realistic single-cause faults |
| visible test suite time | ≤ 15 s per task command | the oracle validation runs every task several times |

## 3. Repositories

Surveyed 2026-09-19 in the cloud workspace: cloned at the tag, test suite run
with pytest 9, Python 3.11. Non-blank lines, source files outside the test
directories.

| repository | pinned | source | tests | deps | suite | release | build |
|---|---|---|---|---|---|---|---|
| [click](https://github.com/pallets/click) 8.3.0 | `00fadb8904387158ce6e9aa1573be770446895c1` | 17 files, 8,935 lines | 30 files, 1,285 tests, 2.2 s | none (colorama on Windows only) | `tests/` | 2025-09-12 | flit, static version |
| [rich](https://github.com/Textualize/rich) 14.1.0 | `2dca1b70359dac61e1bbfb6f14ebe19a5ab79c3d` | 78 files, 23,488 lines | 66 files, 856 tests, 5.6 s (24 skipped) | pygments, markdown-it-py (→ mdurl) | `tests/` | 2025-07-25 | poetry-core, static version |
| [jinja](https://github.com/pallets/jinja) 3.1.6 | `15206881c006c79667fe5154fe80c01c65410679` | 25 files, 11,692 lines | 24 files, 784 tests, 1.4 s without the two async files (they need `trio`) | MarkupSafe | `tests/` | 2025-03-05 | flit, dynamic version from `__init__` |
| [sqlparse](https://github.com/andialbrecht/sqlparse) 0.5.3 | `ec0af5bf6345750d84274bc5c857d4a75b88619b` | 21 files, 3,338 lines | 11 files, 461 tests, 1.0 s | none | `tests/` (+ `tests/files/*.sql` fixtures) | 2024-12-10 | hatchling, dynamic version |

For scale, v0: cachetools 5 files / 1,327 lines, tenacity 13 / 1,853, toolz
14 / 3,034. All four new repositories are pure Python, permissively licensed
(BSD-3 click/jinja, MIT rich, BSD sqlparse), install with `pip install -e .`
at image build time (network on, as today; `pip install -e . trio` for jinja
if an async test file is ever in a task command), and run under `--network
none` afterwards.

Why these four (and not the others surveyed — lark, boltons, packaging,
marshmallow, more-itertools, pyflakes, cerberus, tomlkit, isort, werkzeug,
structlog, attrs): two are **pipelines** whose public entry point is several
modules away from most of the code — jinja (`Environment.from_string(...)
.render()` → lexer → parser → idtracking/compiler → runtime → filters) and
sqlparse (`sqlparse.format()` / `parse()` / `split()` → lexer/keywords →
statement splitter → grouping → filters) — so a symptom described at the
entry point is *cross-module by construction*; **click** has a clear layering
(`decorators` → `core` → `parser`/`types`/`formatting`) where a user's report
names a decorator and the fault sits two modules down; **rich** is an order of
magnitude larger than anything in v0, with a rendering pipeline (`Text`/`Table`
→ `measure`/`_ratio`/`_wrap` → `segment`/`cells` → `Console`) whose faults
surface as wrong output. Rejected: lark (a fine pipeline, kept as the reserve
if one of the four disappoints), boltons (large but its modules are
independent), packaging (tests need `pretend`; small), isort (tests need the
installed distribution's metadata plus hypothesis), werkzeug/attrs/structlog
(heavier test dependencies), tomlkit (test data in a git submodule the
exporter does not fetch), more-itertools/pyflakes (one or two big modules).

Survey notes the authoring will need (extended in
`docs/benchmark-authoring.md` as tasks are built):

- **click**: tests drive commands through `click.testing.CliRunner`; output
  assertions compare `result.output`. `tests/test_options.py` (78 tests) and
  `tests/test_arguments.py` cover `parser.py`/`core.py`; `tests/test_types.py`
  covers `types.py`; `tests/test_formatting.py` the help formatter;
  `tests/test_utils.py` `echo`/`format_filename`. Terminal-dependent code
  (`termui.py`, `_termui_impl.py`, `shell_completion.py`) is out of scope.
- **rich**: tests render into a `Console(file=io.StringIO(), width=N,
  force_terminal=..., color_system=...)` and compare strings; keep every hidden
  test explicit about width, `legacy_windows=False` and colour system, or the
  verdict depends on the host. `tests/render.py` has the helper the suite uses.
  `_emoji_codes.py` (3.6k lines of data) is never a site.
- **jinja**: `Environment()` fixture `env` in `tests/conftest.py`; the lexer
  and parser tests live in `test_lexnparse.py` (107 tests), filters in
  `test_filters.py` (99), loops/`LoopContext` in `test_core_tags.py`,
  scoping in `test_regression.py`/`test_idtracking.py`. Never put
  `tests/test_async.py` or `tests/test_async_filters.py` in a task command.
- **sqlparse**: `tests/test_format.py` (61) and `tests/test_grouping.py` (62)
  are the two natural targets; `tests/test_split.py` for the statement
  splitter; `tests/test_tokenize.py`/`test_keywords.py` for the lexer. Fixture
  SQL files live in `tests/files/`. `sqlparse.format()` is the single public
  entry point through which almost every fault is visible.

## 4. Controlled mutations (32)

### 4.1 Site rules (leak channels 3 and 4)

A site is rejected if any of these hold, checked when the task is designed
and again by the site audit (§6.3), which prints the evidence:

1. A comment or docstring within 8 lines of the hunk states the behaviour the
   mutation breaks ("surface the underlying exception", "missing keys are
   ignored"). Docstrings that describe the *interface* are fine; docstrings
   that state the *invariant being broken* are not.
2. The repository's changelog (`CHANGES.rst`, `CHANGELOG.md`, `CHANGELOG`)
   at the pinned commit describes the behaviour with the site's name in it.
3. The mutated line is one of the banned shapes: a mutable default argument,
   `if not x` for a `None` check, `type(x) == T` / `type(x) in` for
   `isinstance`, an unguarded `del d[k]`, `>` ↔ `>=` on an obviously named
   bound (`stop_after_attempt`), `==` ↔ `is` on a singleton, a bare `except:`.
   These are the patterns a model flags without reading the report.
4. The identifier of the mutated function *is* the behaviour (`LRUCache`,
   `sliding_window`, `stop_after_attempt`) **and** the report names it.
   Either the report stays at the entry point or the site moves.

### 4.2 Mutation catalogue

Faults a maintainer could ship in this code, with the category they file
under (spec §10.2; two categories added in §7):

| kind | example shape | category |
|---|---|---|
| domain boundary | a width computed one cell too wide, a range check that excludes its endpoint in one of two branches, an index that skips the first token of a group | `off_by_one` |
| lost propagation | a keyword accepted by the public constructor and dropped before the helper that needs it; a flag threaded through one of two call paths | `propagation` (new) |
| normalized in one place only | option names normalized when declared but not when matched; a key lower-cased on insert and not on lookup | `wrong_condition` |
| order and stability | tokens grouped before the group they belong in; a sort that loses stability; a filter applied before the one it must follow | `ordering` (new) |
| one path of two | the fast path handles a case the slow path does not (or vice versa); a `cached` branch and an uncached branch that disagree | `wrong_condition` |
| forgotten reset or copy | state carried across two renders; a list shared between two rows; a counter not cleared on re-entry | `state_management` |
| a guard too narrow or too wide | an empty-string or zero value treated as missing; a whitespace token counted as content | `missing_check` |
| exception boundary | the wrong exception type caught on one path, an error re-raised without its context, a failure turned into a silent default | `exception_handling` |
| a cache that outlives its key | memoized result keyed on part of the input; an invalidation missed on one mutation path | `cache_invalidation` |

`retry_logic` stays for tenacity (v0) and is not expected in v1-new; `other`
stays unused.

**Shapes** (derived from the patch, §7): `single_line`, `multi_line` (one
hunk, 2–8 lines), `multi_site` (two or more hunks in one file that must change
together — e.g. a flag read in two places), `cross_file` (hunks in two files —
e.g. a parameter dropped between `core.py` and `parser.py`). Target: ≥ 8
multi-site of which ≥ 4 cross-file.

**Hidden-only construction**: pick a site whose behaviour the existing suite
does not pin down — run the visible test command with the mutation in place;
green means hidden-only, and the hidden test is then the *only* fail-to-pass
test. The agent's INITIALIZE reproduction shows a green suite and the runtime's
TEST phase cannot confirm the fix; success depends entirely on reading the
report and the code. Target ≥ 18 of 36. (v0's two hidden-only tasks both
passed because the report named the function; §6 closes that.)

### 4.3 Planned areas (8 per repository)

Provisional: the areas the sites will be chosen from, with the flags each
task is meant to carry. The final list, with ids and actual sites, is
recorded in `docs/benchmark-authoring.md` as tasks are built; a planned area
that yields no clean site is replaced, and the replacement noted.

**click** (`click_001`–`click_008`; surface: `@click.command` /
`@click.option` / `@click.argument` and `CliRunner` output)

| # | area (module) | fault kind | flags |
|---|---|---|---|
| 1 | option matching, `parser.py` (`_match_long_opt` / `_normalize_opt`) | normalized in one place only | cross-module, visible |
| 2 | `nargs` / multiple value collection, `parser.py` (`_unpack_args`) | domain boundary | cross-module, hidden-only, symptom_only |
| 3 | `IntRange` / `FloatRange` clamping, `types.py` (`_NumberRangeBase.convert`, `_clamp`) | domain boundary | same-module surface, hidden-only, multi-site |
| 4 | `Choice` normalization, `types.py` (`normalize_choice`, `convert`) | normalized in one place only | hidden-only |
| 5 | help formatting, `formatting.py` (`measure_table` / `wrap_text` / `write_dl`) | domain boundary | cross-module, visible |
| 6 | default values and `show_default`, `core.py` (`Option.get_default` / `get_help_record`) | one path of two | hidden-only |
| 7 | parameter processing order / `is_eager`, `core.py` (`iter_params_for_processing`) | order and stability | cross-module, visible, symptom_only |
| 8 | `echo` / `format_filename`, `utils.py` | a guard too narrow or too wide | same-module, hidden-only |

**rich** (`rich_001`–`rich_008`; surface: `Console.print`, `Table`, `Text`,
`Panel`, `Columns`)

| # | area (module) | fault kind | flags |
|---|---|---|---|
| 1 | cell width, `cells.py` (`set_cell_size` / `chop_cells`) | domain boundary | cross-module (surface `Table`/`Text`), hidden-only |
| 2 | segment splitting/cropping, `segment.py` (`split_and_crop_lines`, `adjust_line_length`) | one path of two | cross-module (surface `Panel`/`Console`), visible |
| 3 | word wrapping, `_wrap.py` (`divide_line`) | domain boundary | cross-module (surface `Text`/`Console.print`), hidden-only, symptom_only |
| 4 | ratio distribution, `_ratio.py` (`ratio_resolve` / `ratio_reduce`) | order and stability / off-by-one | cross-module (surface `Table` column widths), hidden-only |
| 5 | column width collapse, `table.py` (`_calculate_column_widths` / `_collapse_widths`) | lost propagation (`min_width`/`max_width`/`no_wrap`) | same-module surface, visible, multi-site |
| 6 | markup parsing, `markup.py` (`_parse` / `render`) | normalized in one place only (closing tags) | cross-module (surface `Console.print("[bold]...")`), visible |
| 7 | style combination, `style.py` (`Style.__add__` / `combine` / `chain`) | forgotten reset or copy | cross-module (surface `Text.stylize` + `Console`), hidden-only |
| 8 | alignment/padding, `align.py` / `padding.py` (`Align.__rich_console__`, `Padding.unpack`) | domain boundary | cross-module (surface `Panel`/`Align`), hidden-only, symptom_only |

**jinja** (`jinja_001`–`jinja_008`; surface: `Environment.from_string(...)
.render(...)` and template source)

| # | area (module) | fault kind | flags |
|---|---|---|---|
| 1 | whitespace control, `lexer.py` (`tokeniter`, `lstrip_blocks` / `trim_blocks`) | one path of two | cross-module, visible, symptom_only |
| 2 | filter/test argument parsing, `parser.py` (`parse_filter` / `parse_test`) | domain boundary | cross-module, hidden-only |
| 3 | loop context, `runtime.py` (`LoopContext.revindex` / `last` / `cycle` / `changed`) | domain boundary / state | cross-module, visible |
| 4 | string filters, `filters.py` (`do_truncate` / `do_wordwrap` / `do_center` / `do_indent`) | domain boundary | same-module surface (filter named in the template), hidden-only |
| 5 | grouping filters, `filters.py` (`sync_do_groupby` / `do_batch` / `sync_do_slice` / `do_dictsort`) | order and stability | hidden-only |
| 6 | scoping, `idtracking.py` (`FrameSymbolVisitor.visit_If` / `branch_update`) | one path of two | cross-module, visible, multi-site, symptom_only |
| 7 | undefined handling, `runtime.py` (`Undefined` / `ChainableUndefined` / `StrictUndefined`) | exception boundary | cross-module, hidden-only |
| 8 | `LRUCache` / `Cycler` / `urlize`, `utils.py` | a cache that outlives its key / forgotten reset | cross-module (surface `Environment` template cache), hidden-only |

**sqlparse** (`sqlparse_001`–`sqlparse_008`; surface: `sqlparse.format` /
`parse` / `split` with SQL text)

| # | area (module) | fault kind | flags |
|---|---|---|---|
| 1 | identifier grouping, `engine/grouping.py` (`group_identifier` / `group_identifier_list`) | domain boundary | cross-module (surface `format(..., reindent=True)`), visible |
| 2 | comparison / operator grouping, `engine/grouping.py` (`group_comparison` / `group_operator`) | one path of two | cross-module, hidden-only |
| 3 | statement splitting, `engine/statement_splitter.py` (`_change_splitlevel`) | domain boundary (BEGIN/CASE levels) | cross-module (surface `split`), hidden-only, symptom_only |
| 4 | whitespace stripping, `filters/others.py` (`StripWhitespaceFilter`) | one path of two (subgroups) | cross-module, visible |
| 5 | reindent, `filters/reindent.py` (`_process_identifierlist` / `_split_kwds` / `_process_case`) | domain boundary | cross-module, hidden-only, multi-site |
| 6 | token navigation, `sql.py` (`TokenList.token_next` / `token_prev` skipping whitespace/comments) | a guard too narrow or too wide | cross-module (surface `format(..., reindent=True)` output with a comment in the statement), hidden-only |
| 7 | keyword lookup, `keywords.py` / `lexer.py` (`is_keyword`, regex order) | order and stability | cross-module (surface `format(keyword_case=...)`), visible, symptom_only |
| 8 | comment stripping, `filters/others.py` (`StripCommentsFilter._process`) | forgotten reset or copy | cross-module, hidden-only |

Flag counts in this plan — hidden-only 20, cross-module 25, symptom_only 8
(+ 4 among the real fixes → 12), multi-site 4 planned explicitly (the rest
come from sites that turn out to need two hunks; the target of 8 is checked
at the end and the last tasks of each repository are chosen to meet it).

## 5. Real fixes (4)

One per repository, `source: real` — the schema has carried the field since
Phase 0 and no task has used it. Selection rule: a fix commit before the
pinned tag whose source change is ≤ 40 lines in ≤ 3 files, that adds tests in
the same commit, and whose subject or changelog line describes a user-visible
misbehaviour (not a refactor, a typing fix or a platform-specific fix).
Candidates found in the histories on 2026-09-19 (ancestor of the pinned tag
in every case):

| repo | fix commit | date | source change | what a user saw |
|---|---|---|---|---|
| click | `4fd2fea0db` | 2025-05-20 | `core.py` +3 −2 | a flag option declared with an explicit `type` got no flag value (issues 2894/2897) |
| click | `1a4d8c1bb1` | 2024-11-02 | `types.py` +7 −1 | `Choice` metavar wrong with `show_choices=False` |
| click | `61f8101f4e` | 2024-05-20 | `core.py` +2 | an empty-string default not shown in help |
| rich | `f2ee29531b` | 2024-09-06 | `text.py` +2 −2 | appending a `Text` to itself never returned |
| rich | `a8c3b8700b` | 2024-09-28 | `segment.py` +8 −2 | cells split wrongly for wide characters |
| rich | `16b3830408` | 2024-10-03 | `table.py` +1 −1 | columns added by `add_row` not highlighted |
| jinja | `051df10c7b` | 2023-06-01 | `parser.py` +8 −6 | a `required` block containing only whitespace or a comment was rejected (or accepted) wrongly at compile time |
| jinja | `e45bc745a7` | 2024-12-19 | `environment.py` +5 −2 | `Environment.overlay()` lost `enable_async` |
| sqlparse | `791e25de46` | 2024-07-15 | `engine/statement_splitter.py` +8 −5 | `split()` cut a `BEGIN ... END` block short when it held more than one `CASE ... END` (issue 784) |
| sqlparse | `0c4902f3f7` | 2024-07-15 | `filters/others.py` +4 | `strip_whitespace` left whitespace inside sub-groups |
| sqlparse | `39b5a02551` | 2023-11-06 | `engine/grouping.py` +1 | nested ordered identifiers not grouped |

One is chosen per repository when `make_task --fix-commit` (§8) validates
it; the first choice is the top row of each group, the others are the
fallbacks. Packaging of a real task:

- `base_commit` = the fix commit's parent (the bug is present; its own tests
  pass there, which `make_task` checks); `gold_patch` = the fix commit's
  source hunks; a per-task image at that commit.
- Hidden tests = the tests the fix commit added, transplanted **into a new
  file** `tests/test_repopilot_<id>.py` (imports and fixtures copied), so the
  invariant "hidden patches only add files" holds for real tasks too and the
  upstream test edit never has to merge with a candidate patch.
- `fail_to_pass` / `pass_to_pass` derived exactly as for mutations, from the
  parent commit with and without the gold patch.
- The report is **written by us**, at the `public_api` or `symptom_only` tier,
  from what the upstream issue reports as observed behaviour — never the
  issue text, never the changelog line. The audit of §6 applies unchanged.
- The changelog entry the fix adds is not in the base tree (it arrives with
  the fix). The repository's earlier release notes are.

What real tasks add: faults nobody designed to be findable. What they cost:
they are in the training data of every model released after them (the probe
of §9.3 measures whether that matters), and they are the four tasks most
likely to be solved by recall rather than by reading. Both are reported per
task, not averaged away.

## 6. Reports

### 6.1 Tiers (`report_level`, authored in `task.toml`)

- `internal` — names the changed function, method or class. v0's tier; no
  v1-new task uses it.
- `public_api` — names only documented public API a user would type in the
  reproduction (`click.option`, `Table.add_column`, the `truncate` filter,
  `sqlparse.format`), never the changed symbol or anything with a leading
  underscore. The default for v1-new (24 tasks).
- `symptom_only` — names nothing below the entry points listed in
  `task.toml` `entry_points` (at most three: e.g. `sqlparse.format`;
  `Environment.from_string` + `render`; `click.command` + `click.option`).
  The reproduction is input data plus observed versus expected output; a CLI
  is reproduced from a shell (`$ prog --count 3`), not through `CliRunner`,
  which is a repository symbol. 12 tasks.

At every tier the report says what a user did, what happened and what was
expected; it never names the fix, never quotes the source, and carries no
traceback frames from inside the package (an exception type and message are
allowed — a user sees those). Difficulty is no longer set by wording (§7).

The same task at the three tiers, for calibration (`toolz_003`, v0):

- internal: "`dissoc` raises `KeyError` when asked to remove a key that is
  not in the dictionary, although the docstring says missing keys are
  ignored." (as shipped)
- public_api: "Removing a key that is not present from a dict with the
  dict-utility function raises `KeyError: 'z'` when the dict has three or
  more keys, and works when it has fewer." — still names nothing below the
  public function, but a public function *is* the gold symbol here, which is
  why a site like this is not eligible for v1 (§4.1 rule 4).
- symptom_only: "`toolz.dicttoolz` — removing an absent key from `{'a': 1,
  'b': 2, 'c': 3}` raises `KeyError: 'z'`; from `{'a': 1}` it returns the
  dict unchanged." — names the entry module only.

### 6.2 Report audit (`evals/benchmark/audit.py`, run by `make_task`, blocking)

Against the *buggy* tree the agent sees, with the symbol table the tools use:

1. No gold symbol name in the report: bare (`token_next`) or qualified
   (`TokenList.token_next`), whole-identifier match, case-insensitive. A
   public alias of a gold symbol is allowed (the `truncate` filter for
   `do_truncate`, `click.option` for `option`): the rule forbids the *name in
   the source*, which is what `search_symbol` and `search_code` find. Sub-token
   overlaps (`token_next` in `token_next_by`) are printed as warnings by the
   site audit, not refused.
2. No gold file name or path (`grouping.py`, `engine/grouping.py`).
3. No traceback frame inside the package (`File ".../<package>/`), no line
   number references.
4. `symptom_only`: every identifier in the report (back-ticked spans,
   dotted names, CamelCase and snake_case words) that resolves to a symbol of
   the repository is one of `entry_points`.
5. `public_api`: every such identifier is public (no leading underscore in
   any path component) and not a gold symbol.

Violations fail the build with the offending span quoted. A warning (not a
failure) when the surface files (§7) intersect the gold files — the task is
same-module, which is allowed but counted.

### 6.3 Site audit (informational, printed by `make_task --dry-run`)

For each hunk: comment and docstring lines within 8 lines that share two or
more sub-tokens with the changed lines; changelog lines at the pinned commit
mentioning any gold symbol; whether the changed line matches a banned shape
(§4.1 rule 3, by regex). The author reads the output and either keeps the
site with a one-line justification in `task.toml` (`site_note`) or moves it.

### 6.4 Redaction (derived, for the ablation arm B′)

`audit.redact(report, symbols)` replaces every identifier that resolves to a
repository symbol (entry points included) with a neutral placeholder — "a
function", "a class", "a method", "a module" — and every back-ticked code
span that contains one with "(code)". Mechanical and reproducible, so arm B′
runs from the task file with a runner switch (§8), no second description to
maintain. On `symptom_only` reports the redaction removes only the entry
points; that arm is reported per tier.

### 6.5 Agreement (unchanged from v0)

Every behaviour a hidden test checks is stated in the report or follows from
it; a different reasonable fix for the described symptom passes the hidden
tests too; when the report is shortened, the matching test goes with it.

## 7. Schema and derivations

Additions to `evals/benchmark/schema.py` (all optional for v0-era files until
the regeneration, then required):

| field | how set | meaning |
|---|---|---|
| `suite` | authored (`v0` / `v1`) | which benchmark generation the task belongs to |
| `report_level` | authored | `internal` / `public_api` / `symptom_only` (§6.1) |
| `entry_points` | authored, `symptom_only` only | the ≤ 3 names the report may use |
| `surface_symbols`, `surface_files` | derived by the audit | repository symbols the report names, and the files defining them (entry points when the report names nothing) |
| `cross_module` | derived | `not (set(surface_files) & set(gold_files))` |
| `hidden_only` | derived | every `fail_to_pass` test is in a hidden test file |
| `shape` | derived from `gold_patch` | `{hunks, files, changed_lines}` and the label `single_line` / `multi_line` / `multi_site` / `cross_file` |
| `difficulty` | **derived** | points = `cross_module` + `hidden_only` + (`multi_site` or `cross_file`) + (`report_level == symptom_only`); 0 → easy, 1 → medium, ≥ 2 → hard |
| `fix_commit` | authored, `real` only | the upstream fix the task was made from (provenance; never shown) |
| `category` | authored | the enum gains `propagation` and `ordering` |

`difficulty` becomes derived because v0's authored labels measured how much
the *wording* gave away — the audit made that a rule instead of a judgement.
Under the v1 rule the v0 tasks come out easy (12), medium (2: `toolz_003`
and `toolz_004`, hidden-only) and hard (0), which is what the leak audit
said about them. (`cachetools_005` is not cross-module by the file rule:
`cached` is *defined* in `_cached.py`, the gold file, and only re-exported
from `__init__`; surface files are the defining files, never the
re-exporting ones.) The v0 `task.toml` files
keep the authored label as `authored_difficulty` for the record; the JSON
carries the derived one. Regenerating the 14 v0 files changes no patch, test,
command or description (`tests/test_benchmark_tasks.py` keeps them consistent
with their sources; the regeneration is one `bench:` commit).

## 8. Harness changes (the build list, in order)

1. `evals/benchmark/schema.py` — the fields of §7; `Category` +
   `propagation`, `ordering`; `Task.hidden_only`, `Task.shape` derived
   properties used by the metrics.
2. `evals/benchmark/audit.py` — report audit, site audit, redaction,
   surface-symbol resolution over `repopilot.tools.symbols`' table of the
   buggy tree; unit tests with a synthetic package.
3. `evals/benchmark/authoring.py` — `make_task` runs the audit before the
   image build (cheap failures first); derives the §7 fields; a `--fix-commit
   <sha>` path for real tasks: exports the parent commit, takes the source
   hunks as `gold_patch`, refuses if the parent's own suite is not green
   under the task command, and expects the transplanted hidden file like any
   other task. `authored_difficulty` accepted in `task.toml`.
4. `scripts/validate_tasks.py` — the suite report: counts against §2's
   targets per suite and repository, the audit re-run over every task,
   `leak_scan.py`'s hidden-name scan folded in.
5. `evals/runner.py` — `--suite {v0,v1,v1-new}` (with `--ids` still
   available); `--report {full,redacted,generic}` applied in
   `AgentSolver.solve` before the `TaskInput` is built (generic = "There is
   one injected bug in this repository; find and fix it."); `--no-run-tests`
   for the baseline solver (drops the tool from the toolbox specs; the
   structured runtime owns its tests and ignores the switch with a warning).
   The run header and `summary.json` record all three.
6. `evals/metrics.py` — breakdowns by `suite`, repository, `hidden_only`,
   `cross_module`, `report_level`, `shape`; `first_edit_in_gold_file` and
   `gold_file_read_before_first_edit` per run, from the traces (the leak
   audit's two localization measures, made standard); `classify_failure`
   gains `localization` (no gold file ever read) ahead of `wrong_patch`.
7. `scripts/memorization_probe.py` — §9.3.
8. `scripts/retrieval_eval.py` — breakdown by suite, `report_level`,
   `cross_module`; unchanged otherwise.
9. `docs/benchmark-authoring.md` — the v1 workflow (the cloud workspace can
   now clone the repositories and run their visible suites, so a task's
   `bug.patch`, `hidden.patch` and description arrive on the Mac ready for
   `make_task`; Marco's part is the Docker derivation, the review of the
   report, and the commit).

Nothing in the agent, the runtime, the tools or retrieval changes for v1.
The benchmark is the treatment; the agents are held fixed.

## 9. Acceptance and experiments (pre-registered)

### 9.1 Acceptance (Phase 0's bar, on 50)

`--solver null --expect fail` → 0 / 50; `--solver gold --expect pass --repeat
2` → 100 / 100 with identical per-test outcomes; `validate_tasks.py` clean:
audit passes for every task, structural targets of §2 met or the shortfall
stated; `leak_scan.py` over the acceptance traces finds no hidden name.
Archived as `bench-v1-acceptance`.

### 9.2 Leak ablation (the audit's §4, run at last)

Baseline agent (the tool loop the audit was written for; the structured
runtime cannot give up its tests), `gpt-5.6-luna` (14 / 14 on v0 at
$0.003 per task), one run per arm on v0 (14) and on v1-new (36):

| arm | report | `run_tests` | isolates |
|---|---|---|---|
| A | full | yes | today's number |
| B′ | redacted (§6.4) | yes | leak 1, identifiers |
| C | full | no | leak 2, visible tests (on hidden-only tasks C ≡ A; reported on the visible-failure subset) |
| D | generic | no | leaks 3–5: is the fault findable from the code alone? |

Expected (success rate, pp = percentage points):

| | v0 (14) | v1-new (36) |
|---|---|---|
| A | 13–14 / 14 | 55–80 % |
| A − B′ | ≥ 20 pp (the identifier is how v0 is solved) | ≤ 10 pp (there is little to redact) |
| A − C, visible-failure tasks | 10–30 pp | 10–30 pp |
| D | ≥ 60 % (sites give themselves away) | ≤ 30 % |
| A − D | ≤ 20 pp | ≥ 30 pp |

Reading: if D stays within 15 pp of A on v1-new, the sites are still
self-describing and v1.1 must change *what* is mutated before anything else
is measured on it. If B′ costs more than 10 pp on v1-new, the reports still
carry identifiers the audit does not catch. Cost: 4 arms × 50 tasks ≈ $1.

### 9.3 Memorization probe

For every gold symbol of every task (v0 and v1-new, real tasks included),
one model call with no tools: the package name, version, file path and the
`def`/`class` line, asking for the body. Similarity to the pinned source =
`difflib.SequenceMatcher` ratio over normalized tokens; "recalled" at ≥ 0.9,
"partial" at 0.6–0.9. Three models (Haiku, Sonnet, luna), ≈ 60 symbols each,
under $1. Reported as the recalled share per suite, and success in arm D
conditioned on recall. Expected: recall higher on v0 (small, old, widely
depended-on libraries) than on v1-new; on the real fixes, recall of the
*fixed* version by models released after the fix is the number to watch. No
expectation is set for the conditional success — it is the measurement.

### 9.4 Offline retrieval on v1-new (`scripts/retrieval_eval.py`)

Same protocol as Phase 3 §4 (report as the query, gold files / symbols as
the relevant set), `LocalEmbedder`, per channel and fusion, broken down by
`report_level` and `cross_module`. Expected: fused Recall@10 (file) 0.55–0.80
(v0: 0.93), MRR (file) 0.35–0.60 (v0: 0.93); dense above BM25 by ≥ 0.10 on
`symptom_only` tasks (the lexical channel has nothing to match); the symbol
channel near zero on `symptom_only` by construction; on `real` tasks no
prediction. First index of rich ≈ 3–5 minutes on the laptop CPU (≈ 4k
chunks), then cached. Archived as `retrieval-v1`.

### 9.5 Agents on v1-new

The current configurations, unchanged:

| run | expected on v1-new (36) |
|---|---|
| structured, compact, Haiku, `--retrieval none` × 2 | success 40–65 %; steps 12–18; tokens per task (median) 45–70k; `budget_tokens` 10–30 %; first edit in a gold file 65–85 % |
| structured, compact, Haiku, `--retrieval evidence` × 2 | success at or above `none` (+0 to +10 pp), with the gain on `cross_module` tasks; LOCALIZE steps below `none` by ≥ 25 %; **tokens below `none` by ≥ 3k at the median** — the Phase 3 question re-asked where localization is not free |
| baseline, Sonnet 5 × 1 | 65–85 % (v0: 100 %) |
| baseline, luna × 1 | 55–80 % (= arm A of §9.2) |
| hidden-only vs visible-failure tasks, any arm | success lower on hidden-only by ≥ 15 pp |
| `real` tasks | success above mutations of the same shape (recall) — reported per task |

If Haiku stays ≥ 90 % on v1-new, the benchmark is still saturated for the
purpose it was built for, whatever the ablation says. If the `evidence` arm
does not beat `none` on success on `cross_module` tasks, runtime-injected
evidence is a step saver only, on v1 as on v0, and the retrieval work's case
rests on the offline numbers.

### 9.6 Failure taxonomy (spec §11.2, first time with room)

With success below saturation, every failed run is classified —
`localization` (no gold file read), `wrong_hypothesis` (gold file read, edit
elsewhere), `wrong_patch` (gold symbol edited, tests still red),
`regression`, `budget`, `loop`, `tool`, `environment` — by `classify_failure`
from the trace, and the distribution per configuration goes in the README
(the audit's rule: if most failures never reach the gold file, patch
generation is not the next lever).

## 10. Plan, effort, cost

| stage | what | who | estimate |
|---|---|---|---|
| 1 harness | §8 items 1–8, tests, docs | Claude builds, Marco runs `uv run pytest` + commits | one session |
| 2 v0 regeneration | 14 tasks under the v1 derivations | Marco: `make_task --force` × 14, `validate_tasks`, commit | 20 min |
| 3 tasks, per repository | 8 mutations + 1 real fix: sites, hidden tests, reports, audits; visible suites run in the cloud before delivery | Claude designs and delivers source dirs; Marco runs `make_task` (Docker), reads each report, commits | one session per repository (≈ 40 min per task on Claude's side; ≈ 10 min on Marco's) |
| 4 acceptance | §9.1, §9.2, §9.3, §9.4 | Marco runs; Claude reads and writes up | one session, ≈ $3 |
| 5 agents | §9.5, README v1 section, failure taxonomy | Marco runs (≈ $12: Haiku 4 × 36 × $0.06, Sonnet 36 × $0.05, luna 36 × $0.005); Claude analyses | one session |

Images: 36 new per-task images plus 4 at real-fix parents; Docker's layer
cache makes every mutation image after a repository's first a few seconds
(same tree, same install layer, different `bug.patch` layer). Retrieval
embeddings: one cold build per repository.

The order of §15.1 in the spec is kept: nothing about the agent changes
until the benchmark can measure it.

## 11. Not in v1

- The prompt-injection security set (spec §9.4) — its own phase.
- More than 50 tasks, or repositories with compiled dependencies, network
  tests, or test suites over 15 s per task command.
- Reports at the `internal` tier for new tasks; tasks whose fix is in test
  files; tasks whose only oracle is an LLM judge.
- Reranking, a second embedding model, or any retrieval change — the
  offline numbers on v1 decide whether §12.1's "+ reranker" row is worth a
  run.
- Tuning the runtime or the evidence block on v1 results; that is Phase 3.2
  (`evidence_k 3`, evidence at PLAN only), after v1 has been measured once.
