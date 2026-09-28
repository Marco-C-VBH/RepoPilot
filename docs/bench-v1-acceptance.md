# Bench v1 — acceptance run sheet (stage 4)

Pre-registered in `docs/bench-v1-design.md` §9.1–9.4; this sheet fixes the
exact commands, the archive names and the expected numbers before anything
is run, and gets the measured numbers filled in afterwards. Benchmark state at
the time of writing: 50 tasks (v0 14 + v1-new 36), all valid, every §2 target
met (`validate_tasks.py`, 2026-09-26). Everything below runs on the Mac
(Docker + `.env`); the cloud session reads the pasted summaries. Total cost
≈ $3 (luna ablation ≈ $1, memorization probe < $1, the rest is free).

Run in this order; each step's archive is the input the README section will
cite. Do not change a task between steps — if one has to change, start over
from step 1.

## 1. Oracles and integrity (free, ~30 min of Docker time)

```
uv run python scripts/validate_tasks.py --audit --strict
uv run python -m evals.runner --solver null --expect fail
uv run python -m evals.runner --solver gold --expect pass --repeat 2
uv run python scripts/leak_scan.py
uv run python scripts/archive_run.py results/<null run dir> bench-v1-null
uv run python scripts/archive_run.py results/<gold run dir> bench-v1-gold-x2
```

| check | expected | measured |
|---|---|---|
| `validate_tasks.py --audit --strict` | exit 0, 50 valid, report audit clean for every task, 8/8 targets ok | ✓ (2026-09-26) |
| null | 0 / 50 pass, exit 0 under `--expect fail` | ✓ 0 / 50 (`null-20260926-092254-1ef4`, 78 s) |
| gold × 2 | 100 / 100 pass, identical per-test outcomes between the two repeats | ✓ 100 / 100, `deterministic: true` (`gold-20260926-092538-5685`, 98 s) |
| `leak_scan.py` over the two runs | no hidden test file or hidden test name in any trace | ✓ clean |

A gold failure means a task whose fixed image differs from the derivation
image (rebuild with `--rebuild` and re-derive with `make_task --force`); a
null pass means a task whose hidden tests pass without the fix (the task is
wrong, not the run).

## 2. Leak ablation (design §9.2; baseline agent, `gpt-5.6-luna`, ≈ $1)

Four arms × two suites, one run each. The baseline agent is the tool loop the
audit was written for; `--no-run-tests` withholds its test tool, `--report`
swaps the bug report.

```
M=gpt-5.6-luna
uv run python -m evals.runner --solver baseline --model $M --suite v0     --max-run-cost 2                                 # A
uv run python -m evals.runner --solver baseline --model $M --suite v1-new --max-run-cost 2                                 # A
uv run python -m evals.runner --solver baseline --model $M --suite v0     --max-run-cost 2 --report redacted               # B′
uv run python -m evals.runner --solver baseline --model $M --suite v1-new --max-run-cost 2 --report redacted               # B′
uv run python -m evals.runner --solver baseline --model $M --suite v0     --max-run-cost 2 --no-run-tests                  # C
uv run python -m evals.runner --solver baseline --model $M --suite v1-new --max-run-cost 2 --no-run-tests                  # C
uv run python -m evals.runner --solver baseline --model $M --suite v0     --max-run-cost 2 --report generic --no-run-tests # D
uv run python -m evals.runner --solver baseline --model $M --suite v1-new --max-run-cost 2 --report generic --no-run-tests # D
```

Archive each as `leak-v1-<arm>-<suite>-luna` with `<arm>` in `A`, `Bp`, `C`,
`D` and `<suite>` in `v0`, `v1new` (eight archives). The run header prints
`report` and `run_tests`; check both before reading a number.

| | v0 (14) expected | v0 measured | v1-new (36) expected | v1-new measured |
|---|---|---|---|---|
| A success | 13–14 / 14 | **14 / 14** ✓ | 55–80 % | **31 / 36 = 86.1 %** (above the range) |
| B′ success; A − B′ | ≥ 20 pp below A | 14 / 14; **0 pp** ✗ | ≤ 10 pp below A | 29 / 36 = 80.6 %; **5.5 pp** ✓ |
| C success on visible-failure tasks; A − C | 10–30 pp below A (12 tasks) | 12 / 12; **0 pp** ✗ | 10–30 pp below A (11 tasks) | 8 / 11 vs A 7 / 11; **−9 pp** ✗ (C above A) |
| D success; A − D | D ≥ 60 %; A − D ≤ 20 pp | **6 / 14 = 42.9 %**; A − D **57 pp** ✗ (D lower, gap wider than expected) | D ≤ 30 %; A − D ≥ 30 pp | **2 / 36 = 5.6 %**; A − D **80.5 pp** ✓✓ |

Runs (all `gpt-5.6-luna`, baseline agent, default budget: 30 steps, 40 tool
calls, 5 test runs, 100k tokens, $0.50, 600 s; 2026-09-26): v0 A
`baseline-20260926-092939-b63e`, B′ `-094917-1235`, C `-101023-8614`, D
`-102921-7534`; v1-new A `-093354-1684`, B′ `-095340-11ef`, C `-101420-af8a`,
D `-103358-e071`. Total cost $1.05 (predicted ≈ $1). Per arm on v1-new: cost
$0.18 / $0.17 / $0.20 / $0.27; tokens per task (median) 73k / 69k / 78k /
108k; runs ended by the token cap 10 / 9 / 12 / 29 of 36.

Measured, beyond the table:

- v1-new, arm A: the 5 failures are all `budget_exceeded`, and 4 of them are
  multi-site or cross-file tasks (jinja_008, jinja_009, rich_006,
  sqlparse_001; the fifth is sqlparse_008). By shape: single_line 18 / 19,
  multi_line 6 / 6, multi_site 4 / 6, cross_file 3 / 5. By tier: public_api
  19 / 24, symptom_only 12 / 12. Localization: gold file read in 36 / 36 runs,
  first edit in a gold file in 31 of the 33 runs that edited (94 %).
- v1-new, arm D: 2 passes (click_004, rich_003); failures `budget_exceeded`
  22, `retrieval_failure` 9 (the gold file never read), `incorrect_patch` 3;
  gold file read in 27 / 36 runs, first edit in a gold file in 7 of 10 runs
  that edited. Every multi-site, cross-file and multi-line task fails in D.
- v0, arm D: 6 passes (cachetools_001, cachetools_004, cachetools_005,
  tenacity_003, toolz_002, toolz_003); 9 of 14 runs ended by the token cap.
- Noise floor: on the 25 hidden-only tasks C carries the same information
  as A, yet the two arms score 24 / 25 and 22 / 25 (three tasks flip one
  way, one the other); single runs of this agent move by about ±2 tasks of
  36 (≈ 5 pp). Differences of
  that size (A − B′, A − C) are within noise; the A − D gap is 15× it.

Reading against the rules fixed above. The two decisive rows hold with a
wide margin: without the report the fault is found in 2 of 36 tasks (D =
5.6 %, ceiling 30 %), with it in 31 of 36, so the report is the information
and the sites do not give themselves away; and the report survives redaction
(B′ within 5.5 pp, ceiling 10 pp), so what it carries is the symptom, not
the identifiers. Neither v1.1 trigger fires. The rows that missed are all
on the v0 side of the prediction and all in the same direction: v0 was
expected to be solved *through* its identifiers and its visible tests, and
it is not — B′ and C stay at 14 / 14, and even D reaches 43 % — v0 is solved
from the report *or* the tests *or* the code, whichever is left; only taking
all three away costs anything. The audit's leak ranking (identifiers first)
was wrong for this model; the v0 sites were less self-describing than feared
(43 %, not ≥ 60 %), but eight times more so than v1-new's (5.6 %). On
v1-new, A landed above its 55–80 % range: the baseline with the full report
solves 86 % — not saturated (v0: 100 %), and the failures concentrate on the
multi-site shapes, which is where the benchmark's headroom is. The two D
passes on v1-new (click_004, rich_003) are noted for v1.1: both are
single-line sites an inspection can spot without a report.
C is read on the visible-failure subset only (`by_hidden_only` in
`summary.json`: on hidden-only tasks C ≡ A by construction). Reading rules,
fixed in advance: D within 15 pp of A on v1-new → the sites are still
self-describing, v1.1 changes *what* is mutated before anything else is
measured; A − B′ > 10 pp on v1-new → the reports still carry identifiers the
audit does not catch (fix the audit, re-write the reports, re-run B′ only).
Per-task results of D are kept: they feed §3's conditional reading and the
site list for any v1.1.

## 3. Memorization probe (design §9.3; three models, no tools, < $1)

```
uv run python scripts/memorization_probe.py --models claude-haiku-4-5-20251001 claude-sonnet-5 gpt-5.6-luna --max-cost 2
uv run python scripts/archive_run.py results/<probe dir> memorization-v1
```

| | expected | measured (run 2, `memorization-20260927-110408-8f9b`, archived as `memorization-v1`) |
|---|---|---|
| recalled share (similarity ≥ 0.9), v0 vs v1-new, per model | v0 higher than v1-new for every model | ✓ Haiku 2 / 14 = 14 % vs 0 / 45 = 0 %; Sonnet 7 / 14 = 50 % vs 18 / 45 = 40 %; luna 4 / 14 = 29 % vs 4 / 45 = 9 % |
| real tasks: `ratio_fixed` by models released after the fix | reported per task, no expectation | Sonnet: sqlparse_009 is the one clear case — `TypedLiteral` 0.98 against the fixed tree vs 0.84 against the buggy base, `match` 0.73 vs 0.56; jinja_009's `dump_stores` 1.00 vs 0.98, rich_009 0.90 vs 0.89 and click_009 0.84 vs 0.83 are tiny patches where the two references barely differ. Haiku and luna: none of the six fixed symbols |
| arm-D success conditioned on recall (join with §2's D per task) | the measurement; no expectation | luna, 50 tasks: with a recalled gold symbol D passes 2 / 8, without 6 / 42 (run 1: 4 / 9 vs 4 / 41) — too few recalled tasks for a rate; what is stable is where both live: 5 of luna's 8 recalls and 6 of its 8 D passes are v0 tasks, and the two v1-new D passes (click_004, rich_003) are not recalled by any model |

59 of the 60 gold symbols probed (rich_008's `traverse._traverse` is 252
lines, above the 200-line cap), three models, $0.92, 42 min. Median
similarity: Haiku 0.46 (v0) / 0.30 (v1-new), Sonnet 0.90 / 0.85, luna 0.65 /
0.42. By repository (Sonnet, the only model with recall on v1-new): toolz
4 / 4, sqlparse 7 / 13, jinja 6 / 13, click 4 / 10, tenacity 2 / 5, rich
1 / 9, cachetools 1 / 5.

Replication: run 1 (`memorization-20260926-130404-fbd6`, 35 min, $0.89,
archived as `memorization-v1-run1`) was made before issue #16 was fixed, so
its rich_004 row measures `Console.render` instead of the gold
`markup.render`; everything else is the same protocol. Across the two runs
the recalled counts move by a few symbols per model — Haiku 5 → 2, Sonnet
27 → 25 (5 symbols out, 3 in), luna 10 → 8 — because the 0.9 threshold cuts
through a band of near-verbatim replies (toolz's `unique` is 0.84 in one run
and 1.00 in the other). The per-model ordering and the v0 > v1-new direction
are identical in both runs; single-run counts should be read with a ± 3
margin.

Reading. The pre-registered direction holds for all three models in both
runs: the v0 libraries are the memorized ones; on v1-new Haiku recalls
nothing and luna 4 symbols of 45, and their arm-D results sit with that —
the cheap models cannot find a v1-new fault from memory. Sonnet is the
exception to keep in view: it reproduces 40 % of the v1-new gold symbols
near-verbatim (sqlparse and jinja above all), and for sqlparse_009 it
reproduces the *fixed* code more faithfully than the buggy base, so on that
task a Sonnet agent can write the fix from memory; Sonnet's v1-new success
will be reported with the recalled tasks marked, and sqlparse_009 under
Sonnet is not evidence of debugging. Nested gold functions above the line
cap stay outside the measurement, and one Sonnet reply in run 2 (rich_004)
came back empty and scores 0.

## 4. Offline retrieval (design §9.4; no model, first rich index 3–5 min)

```
uv run python scripts/retrieval_eval.py
uv run python scripts/archive_run.py results/<retrieval dir> retrieval-v1
```

The script runs over all 50 tasks; `summary.json` carries `by_group` for
`suite`, `report_level`, `cross_module` and `hidden_only` — read the v1-new
rows from the `suite` group.

| v1-new (36) | expected | measured |
|---|---|---|
| fused Recall@10 (file) | 0.55–0.80 (v0: 0.93) | **0.86** (bm25+dense; three channels also 0.86) — above the range; v0 0.93 |
| fused MRR (file) | 0.35–0.60 (v0: 0.93) | **0.57** ✓ (bm25+dense; three channels 0.44); v0 0.86 / 0.93 |
| dense − BM25, Recall@10 on `symptom_only` | ≥ +0.10 | **0.00** ✗ (0.58 vs 0.58, n = 12); on MRR dense leads by +0.20 (0.50 vs 0.30) |
| symbol channel on `symptom_only` | near zero | **0.08** ✓ (0.29 on `public_api`, 0.93 on v0) |
| `real` tasks | no prediction, reported | all four found: fused first-hit rank 1 (click_009), 2 (jinja_009), 1 (rich_009), 1 (sqlparse_009) |

Run `retrieval-20260926-115522-07b2` (2026-09-26, `BAAI/bge-small-en-v1.5`,
k = 10, 50 tasks, 55,170 chunks). Index build 892 s in total: rich 299 s,
jinja 249 s, click 133 s, sqlparse 90 s, the real-fix base commits 10–44 s
each, everything else from the cache; a query costs 4.8 ms (BM25) / 68 ms
(dense). Per channel on v1-new: BM25 R@10 0.86 / MRR 0.53, dense 0.78 / 0.58,
symbol 0.22 / 0.14, bm25+dense 0.86 / 0.57, all three 0.86 / 0.44. By
`cross_module`: 1.00 / 0.93 on the 21 same-module tasks, 0.79 / 0.44 on the
29 cross-module ones (fused). By `hidden_only`: 0.85 / 0.60 on the 27
hidden-only tasks.

The five fused misses at k = 10 are click_008 (`_textwrap.py`), jinja_002
(`nodes.py`), rich_002 (`style.py`), rich_005 (`_ratio.py`) and rich_008
(`pretty.py`) — four of them `symptom_only`, and two of them (rich_002,
rich_008) are hits for BM25 alone at rank 10 and 8 that the fusion drops:
the agreement reward of RRF buries a single-channel hit, as it did for
cachetools_005 on v0. On the twelve `symptom_only` tasks the two channels
miss different tasks (dense finds jinja_004 and sqlparse_003, BM25 finds
rich_002 and sqlparse_005), so their Recall@10 ties at 7 / 12 while dense
ranks its hits higher (first hit at rank 1 in 5 of its 7, BM25 in 2 of
its 7); the predicted
dense advantage shows on MRR, not on recall, and the symbol channel is
irrelevant there as predicted. The symbol channel's collapse (0.93 on v0 →
0.22 on v1-new) is the audit's report rule at work: the reports no longer
name the changed symbol, so a channel that matches identifiers has nothing
to match. Recall@10 landing above the predicted range means the reports
still locate the file more often than expected — it is the *rank* that got
harder (MRR 0.57 vs 0.86), which is what the agent-level `evidence` arm will
have to work with.
## What goes forward

Step 1 is the acceptance bar (design §9.1): all four rows green or the
benchmark is not released. Steps 2–4 are measurements with pre-registered
readings; a miss is a finding, not a failure — it goes in the README v1
section next to the expectation, and decides between "measure the agents on
v1 as is" (stage 5) and "v1.1 sites first".
