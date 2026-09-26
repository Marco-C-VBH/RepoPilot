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

| | expected | measured |
|---|---|---|
| recalled share (similarity ≥ 0.9), v0 vs v1-new, per model | v0 higher than v1-new for every model | |
| real tasks: `ratio_fixed` by models released after the fix | reported per task, no expectation | |
| arm-D success conditioned on recall (join with §2's D per task) | the measurement; no expectation | |

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
| fused Recall@10 (file) | 0.55–0.80 (v0: 0.93) | |
| fused MRR (file) | 0.35–0.60 (v0: 0.93) | |
| dense − BM25, Recall@10 on `symptom_only` | ≥ +0.10 | |
| symbol channel on `symptom_only` | near zero | |
| `real` tasks | no prediction, reported | |

## What goes forward

Step 1 is the acceptance bar (design §9.1): all four rows green or the
benchmark is not released. Steps 2–4 are measurements with pre-registered
readings; a miss is a finding, not a failure — it goes in the README v1
section next to the expectation, and decides between "measure the agents on
v1 as is" (stage 5) and "v1.1 sites first".
