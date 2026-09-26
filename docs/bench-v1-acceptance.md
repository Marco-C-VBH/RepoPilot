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
| `validate_tasks.py --audit --strict` | exit 0, 50 valid, report audit clean for every task, 8/8 targets ok | |
| null | 0 / 50 pass, exit 0 under `--expect fail` | |
| gold × 2 | 100 / 100 pass, identical per-test outcomes between the two repeats | |
| `leak_scan.py` over the two runs | no hidden test file or hidden test name in any trace | |

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
| A success | 13–14 / 14 | | 55–80 % | |
| B′ success; A − B′ | ≥ 20 pp below A | | ≤ 10 pp below A | |
| C success on visible-failure tasks; A − C | 10–30 pp below A (12 tasks) | | 10–30 pp below A (11 tasks) | |
| D success; A − D | D ≥ 60 %; A − D ≤ 20 pp | | D ≤ 30 %; A − D ≥ 30 pp | |

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
