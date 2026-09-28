# Bench v1 — agent runs (stage 5) run sheet

Pre-registered in `docs/bench-v1-design.md` §9.5–9.6; this sheet fixes the
commands, archive names and expected numbers before the runs, and takes the
measured numbers afterwards. Inputs: the 36 v1-new tasks (50 with v0),
accepted on 2026-09-26 (`docs/bench-v1-acceptance.md`: null 0 / 50, gold
100 / 100, leak ablation D = 5.6 % on v1-new, redaction −5.5 pp). The luna
baseline already exists — it is arm A of the ablation
(`leak-v1-A-v1new-luna`, 31 / 36) — and is not re-run.

Configurations are the Phase 2/3 ones, unchanged: default budget (30 steps,
40 tool calls, 5 test runs, 100k tokens, $0.50, 600 s per task), compact
context with K = 3, local embedder, all three channels for the evidence arm.
Every run is archived; the README v1 section cites archives only.

## Order, commands, caps

Haiku first (the model the runtime work was measured on), `none` before
`evidence` so the control exists before the treatment; Sonnet last. Caps are
per run and generous: v1-new tasks cost luna 3–4× a v0 task (median 73k
tokens vs 40k), so a Haiku run of 36 tasks is expected around $4–5 and two
repeats $8–10; stop a run early only if the cap trips, and say so here.

```
H=claude-haiku-4-5-20251001
# 1. structured, compact, no retrieval, Haiku × 2       (~2 h, ≤ $10)
uv run python -m evals.runner --solver structured --model $H --context compact --retrieval none --suite v1-new --repeat 2 --max-run-cost 10
# 2. structured, compact, evidence at PLAN, Haiku × 2   (~2 h, ≤ $10)
uv run python -m evals.runner --solver structured --model $H --context compact --retrieval evidence --suite v1-new --repeat 2 --max-run-cost 10
# 3. baseline, Sonnet 5 × 1                             (~1 h, ≤ $8)
uv run python -m evals.runner --solver baseline --model claude-sonnet-5 --suite v1-new --max-run-cost 8
# 4. baseline, Haiku × 2 — the same-model control, pre-registered below after 1–3 were read (~40 min, ≤ $10)
uv run python -m evals.runner --solver baseline --model $H --suite v1-new --repeat 2 --max-run-cost 10
```

Archive as `structured-v1-compact-none-haiku45-x2`,
`structured-v1-compact-evidence-haiku45-x2`, `baseline-v1-sonnet5`,
`baseline-v1-haiku45-x2`. After each batch: `uv run python
scripts/leak_scan.py` (no hidden test name in any trace), then the README v1
section.

## Expected (design §9.5) and measured

| run | expected on v1-new (36) | measured |
|---|---|---|
| Haiku structured compact `none` × 2 | success 40–65 %; steps 12–18; tokens per task (median) 45–70k; `budget_tokens` terminations 10–30 %; first edit in a gold file 65–85 % | **51 / 72 = 70.8 %** (repeats 25 / 36 and 26 / 36; above the range, far from the 90 % saturation line); steps 15.6 ✓; tokens median 64.4k ✓ (mean 71.5k); budget_tokens 24 / 72 = 33 % (just above); first edit in a gold file 58 / 68 = 85 % ✓; gold file read in 72 / 72; $6.04 total, $0.076 median per task, 52 min (`structured-20260928-000838-ceb2`) |
| Haiku structured compact `evidence` × 2 | success at or above `none` (+0 to +10 pp), the gain on `cross_module` tasks; LOCALIZE steps ≥ 25 % below `none`; tokens ≥ 3k below `none` at the median | **49 / 72 = 68.1 %** (repeats 25 / 36 and 24 / 36; two runs below `none`'s 51 / 72 — inside the ± 2-task band, so a tie, but not "at or above") ✗; `cross_module` 36 / 56 = 64 % against 42 / 56 = 75 % under `none` — the predicted gain is absent and the direction is negative (same-module 13 / 16 against 9 / 16) ✗; LOCALIZE steps 570 against 603 = −5.5 % ✗ (steps per run 14.0 against 15.6, tool calls 13.0 against 15.7); tokens median 74.0k against 64.4k = **+9.6k** ✗ (mean 77.2k against 71.5k; paired per task +2.9k at the median); budget_tokens 25 / 72; first edit in a gold file 57 / 65 = 88 %; gold file read 70 / 72; evidence 3.4k chars median (2.4k–4.0k), 93 ms median per retrieval; $6.38 total, $0.085 median per task, 52 min of run time (`structured-20260928-010214-3d66`) |
| Sonnet 5 baseline × 1 | 65–85 % (v0: 100 %) | **31 / 36 = 86.1 %** (1 pp above the range; the same count as luna, overlapping on 2 of the 5 failures — jinja_008, rich_006); 30 / 35 = 85.7 % without sqlparse_009, which per the rule below is not counted; recall marking: 16 tasks have at least one gold symbol Sonnet recalled in `memorization-v1` (11 all of them) and Sonnet passes 13 / 16 of those against 18 / 20 of the rest — recall does not carry the number; all 5 failures `budget_exceeded`, 4 of them without a single edit; localization 32 / 32 first edits in a gold file, gold file read 36 / 36; steps 9.3, tool calls 9.7, test runs 1.2; tokens median 49.1k (mean 57.4k; 20k below Haiku `none` per task, paired median); budget_tokens 7 / 36; $4.72 total, $0.119 median per task, 19 min (`baseline-20260928-024416-1470`) |
| luna baseline × 1 (= ablation arm A) | 55–80 % | 31 / 36 = 86.1 % (2026-09-26; above the range) |
| Haiku baseline × 2 (follow-up, pre-registered at the end of this sheet before it ran) | success within ± 5 pp of `none` (a tie, as on v0); median tokens ≥ 1.5× `none`; `budget_tokens` share well above `none`'s 33 % | **26 / 72 = 36.1 %** (13 + 13; **−34.7 pp** against `none`'s 70.8 % — the tie is falsified, in the runtime's favour) ✗; tokens median 106.5k = 1.66× ✓ (mean 105.7k; every run between 87.8k and 114.6k, pass median = fail median — the distribution is the cap); budget_tokens **70 / 72 = 97 %** ✓ (2 runs ended `done`; 24 of the 70 capped runs still passed); edits in 32 / 72 runs (`none`: 68 / 72), first edit in a gold file 32 / 32, gold file read 68 / 72; steps 16.5, tool calls 16.6, test runs 1.5; input tokens per model call 6.3k against 4.4k under `none` (+44 %) at the same step count; $8.22 total, $0.114 median per task, 36 min (`baseline-20260928-035556-5905`) |
| hidden-only vs visible-failure tasks, any arm | success lower on hidden-only by ≥ 15 pp | Haiku `none`: hidden-only 39 / 50 = 78 %, visible-failure 12 / 22 = 55 % — the **opposite** direction (−23 pp on the visible set) ✗. Confounded by shape: the visible-failure tasks are where the multi-site/cross-file mutations landed (sqlparse_001/002/007/008, jinja_008, rich_006, click_003 …), and shape is the stronger predictor: single_line 29 / 38 = 76 %, multi_line 9 / 12, multi_site 7 / 12, cross_file 6 / 10 (13 / 22 = 59 % for the two multi-site shapes). Haiku `evidence`: the same direction, wider — hidden-only 41 / 50 = 82 %, visible-failure 8 / 22 = 36 %; single_line 28 / 38, multi_line 8 / 12, multi_site 7 / 12, cross_file 6 / 10. Sonnet baseline: hidden-only 23 / 25 = 92 %, visible-failure 8 / 11 = 73 %; single_line 16 / 19, multi_line 6 / 6, multi_site 4 / 6, cross_file 5 / 5. Three arms, one direction: the prediction is wrong as stated, and the visible-failure set is the harder one because of what was put in it. Haiku baseline: hidden-only 17 / 50 = 34 %, visible-failure 9 / 22 = 41 % — the first arm in the predicted direction, by 7 pp (≈ 1.5 tasks, inside the noise band) |
| `real` tasks (click_009, jinja_009, rich_009, sqlparse_009) | success above mutations of the same shape; per task | Haiku `none`: 7 / 8 (click_009 2 / 2, jinja_009 1 / 2, rich_009 2 / 2, sqlparse_009 2 / 2) vs 44 / 64 = 69 % for the mutations ✓. Two of them were solved *outside the gold files*: sqlparse_009 both times by making `TIMESTAMP` a builtin in `keywords.py` (upstream later did the same), jinja_009 once by sorting at the consumer in `compiler.py` instead of in `idtracking.py` — valid fixes the hidden tests accept, which the gold-file localization metric counts against the agent. Haiku `evidence`: 6 / 8 (click_009, rich_009, sqlparse_009 2 / 2; jinja_009 0 / 2 — the evidence listed `compiler.py` at rank 4 and never `idtracking.py`, both repeats patched `compiler.py` alone and failed) against 43 / 64 = 67 % for the mutations ✓. Sonnet baseline: 4 / 4 against 27 / 32 = 84 % for the mutations ✓; jinja_009 by editing both gold files, sqlparse_009 by editing exactly the two files of the upstream commit (`grouping.py`, `sql.py`) — the commit the probe showed Sonnet can reproduce, so per the rule below it is a recall, not a debugging result. Haiku baseline: 3 / 8 (click_009 1 / 2, rich_009 2 / 2, jinja_009 0 / 2, sqlparse_009 0 / 2 — the two cross-file real tasks, both at the cap) against 23 / 64 = 36 % for the mutations — parity ✗ |

Readings fixed in advance:

- Haiku ≥ 90 % on v1-new → the benchmark is still saturated for what it was
  built for, whatever the ablation said; v1.1 before any runtime work.
- `evidence` not above `none` on `cross_module` success → runtime-injected
  evidence is a step saver only, on v1 as on v0, and the retrieval work's
  case rests on the offline numbers (fused R@10 0.86 / MRR 0.57).
- Sonnet's success is reported with the tasks whose gold symbols the
  memorization probe found recalled (18 of 45 symbols; `memorization-v1`)
  marked, and sqlparse_009 under Sonnet is not counted as evidence of
  debugging (the probe reproduces its fixed code).
- Two repeats of a Haiku arm give the run-to-run noise on v1-new; the luna
  ablation put it near ± 2 tasks of 36 for single runs. Differences between
  arms inside that band are reported as ties.

## Breakdowns to report (from `summary.json`, every run)

`success_rate`, `by_report_level`, `by_hidden_only`, `by_cross_module`,
`by_shape`, `by_repo`, `by_category`; `localization` (gold file read, first
edit in a gold file); `terminations` and `failures` (the taxonomy of §9.6:
`localization` / `retrieval_failure`, `wrong_hypothesis`, `wrong_patch` /
`incorrect_patch`, `regression_introduced`, `budget_exceeded`, `agent_loop`,
`tool`, `environment`); tokens and cost (median, mean, total); steps, tool
calls, test runs; for the structured arms the phase counts (PLAN / LOCALIZE /
PATCH / TEST) and, for `evidence`, `evidence_chars`, PATCH reads that
overlap the evidence, and the retrieval latency.

## Failure taxonomy (design §9.6)

Filled after the runs: one row per configuration, the count of failed runs
per class, and the rule's verdict — if most failures never reach the gold
file, patch generation is not the next lever.

| configuration | failed runs | localization | wrong hypothesis | wrong patch | regression | budget | loop / tool / env |
|---|---|---|---|---|---|---|---|
| Haiku `none` × 2 | 21 / 72 | 1 (`wrong_localization`) | – | 6 (`incorrect_patch`) | 2 | 12 | 0 |
| Haiku `evidence` × 2 | 23 / 72 | 3 (2 `retrieval_failure`, 1 `wrong_localization`) | – | 3 (`incorrect_patch`) | 1 | 16 | 0 |
| Sonnet baseline | 5 / 36 | 0 | – | 0 | 0 | 5 (4 without any edit) | 0 |
| luna baseline (arm A) | 5 / 36 | 0 | 0 | 0 | 0 | 5 | 0 |
| Haiku baseline × 2 | 46 / 72 | 4 (`retrieval_failure`) | – | 0 | 1 | 41 (36 without any edit) | 0 |

Haiku `none` × 2, read on 2026-09-28. Every failed run had read a gold file
(72 / 72), so localization is not the lever on v1-new for this
configuration; the failures are the token cap (12, median tokens of failed
runs 101k against 61k for passes; 10 of the 24 capped runs still passed with
the patch they had) and patch quality (6 wrong patches, 2 regressions). The
five tasks that fail in both repeats: jinja_001 — both runs patched
`environment.py` only and left `sandbox.py` alone, the cross-file design
doing exactly what it was built for (the 4 sandbox tests stay red); rich_006,
rich_007, sqlparse_003, sqlparse_008 — all four run out of tokens (rich_006
once without a single edit). Eleven tasks flip between the repeats
(click_001, click_003, jinja_003, jinja_006, jinja_008, jinja_009, rich_001,
rich_002, rich_005, sqlparse_001, sqlparse_007), twenty pass twice: the
aggregate is stable to one task, the per-task verdicts are not — success
comparisons between arms are read on the aggregate, never per task.
Runtime: 0 loops, 5 workspace resets, 69 forced TEST transitions, 59 re-reads
after the K = 3 window, invalid tool calls 1.2 %; steps by phase LOCALIZE 603,
PATCH 397, PLAN 72, FINALIZE 44, ANALYZE 10 — LOCALIZE is 53 % of all steps,
the number the `evidence` arm is meant to move.

Haiku `evidence` × 2, read on 2026-09-28. None of the three pre-registered
effects appeared: success 49 / 72 against 51 / 72 (a tie inside the noise
band, not "at or above"), `cross_module` 36 / 56 against 42 / 56, LOCALIZE
570 steps against 603 (−5.5 %, not −25 %), tokens +9.6k at the median
instead of −3k. The fallback reading fixed above applies as written:
runtime-injected evidence is a step saver only (−1.7 steps and −2.7 tool
calls per run; forced TEST transitions 53 against 69), and the retrieval
work's case rests on the offline numbers. The traces say why, and the why is
the part worth keeping.

1. Recall at the injected k is the offline number's weaker cousin. The five
   chunks taken once at PLAN (≤ 4 000 chars, 93 ms median, 583 ms max)
   contained a gold file for 23 of the 36 tasks — 0.64 at k = 5 chunks,
   against R@10 = 0.86 offline — and all 13 misses are `cross_module` tasks
   (click_001, click_008, jinja_002/003/004/005/008, rich_002/005/007/008,
   sqlparse_003/007), which is what the offline cross-module MRR of 0.44
   said would happen. Every same-module task was a hit.
2. The agent follows the evidence whether or not it is right. The first
   `read_file` is an evidence file in 50 / 72 runs; 64 % of all reads
   (315 / 495) and 66 % of PATCH-phase reads (89 / 134) are evidence files;
   the first edit lands in one in 46 / 65. On the 23 hit tasks that is
   harmless (37 / 46 against 35 / 46 under `none`); on the 13 misses it
   costs (12 / 26 against 16 / 26), and the whole deficit sits there:
   localization failures went from 1 to 3 — jinja_002 and jinja_003 never
   opened `nodes.py` / `utils.py` in one repeat each, rich_005 patched
   `table.py`, the file all five of its chunks came from, instead of
   `_ratio.py`. Injected evidence is trusted, so its precision matters more
   than its recall; evidence that is right 64 % of the time should not be
   injected blind — gating on retrieval confidence, or injecting it as a
   ranked hint the LOCALIZE phase verifies, is the v1.1 runtime question.
3. The block is paid for on every call, not once. Input tokens per model
   call 5.3k mean against 4.4k under `none`, so a run spends +2.9k tokens per
   task (paired median) despite the fewer steps; on the hit tasks the median
   rose from 61k to 71k, on the misses both arms sit at the cap (98k / 99k).
   The K = 3 window compacts tool output, not the evidence block.
4. The design case does occur, once. jinja_001 — the cross-file task that
   failed both `none` repeats with `sandbox.py` untouched — had
   `SandboxedEnvironment` as its rank-1 chunk and passed both repeats with
   both files edited. Its mirror image is sqlparse_001: the evidence listed
   all three gold files (ranks 1, 2 and 4) and both repeats still edited
   `sql.py` alone and ran out of tokens (`none`: 1 / 2). On multi-file tasks
   the cap binds before localization does.

Eight tasks fail both repeats (jinja_008, jinja_009, rich_005, rich_006,
rich_007, rich_008, sqlparse_001, sqlparse_003), 21 pass both, 7 flip. The
per-task movements against `none` (jinja_001 0 → 2; jinja_006, rich_001,
rich_002 1 → 2; sqlparse_008 0 → 1; rich_008 2 → 0; jinja_008, jinja_009,
rich_005, sqlparse_001 1 → 0; jinja_002, rich_004 2 → 1) are inside the flip
rate of `none`'s own repeats and are not read individually — only the hit /
miss split above is. The rest of the profile matches `none`: failed runs'
median tokens 103k against 67k for passes, 7 of the 25 capped runs still
passed; 0 loops, 5 resets, 70 re-reads, invalid tool calls 0.3 %; steps by
phase LOCALIZE 570, PATCH 315, PLAN 72, FINALIZE 42, ANALYZE 8. Timing: 52
min of run time inside 98 min of wall clock — the Mac was asleep for ≈ 46
min in four stretches (`time.monotonic()` stops during sleep on macOS, so
`duration_seconds` and the per-task runtime budget both exclude it, and no
run was affected). The index came from the cache every time (≤ 1.6 s per
task, 0–3 chunks re-embedded, 47 s over the run); retrieval cost is not a
factor either way.

Sonnet 5 baseline × 1, read on 2026-09-28. 31 / 36, one point above the
pre-registered range and the same count as luna (arm A), with two failures
in common (jinja_008, rich_006); Sonnet also fails rich_004, rich_005,
sqlparse_003, luna instead jinja_009, sqlparse_001, sqlparse_008. The
memorization marking does not move the number: the 16 tasks with a recalled
gold symbol go 13 / 16, the other 20 go 18 / 20, and two of the three
recalled failures (rich_005, sqlparse_003) are tasks the probe recalled in
full — being able to write the function from memory did not make the agent
find and fix it within the budget. Without sqlparse_009 (a recall by the
rule above) the rate is 30 / 35 = 85.7 %. All five failures are budget
exhaustion with the gold file already read, and four of them never edited:
the baseline re-sends the whole transcript on every call, so its input
tokens climb step by step (rich_006: 2.3k → 18.4k over 11 calls, 112k in
total, five `search_code no_wrap` calls in a row with no loop detector to
stop them; jinja_008: `compiler.py` read three times and `CHANGES.rst`
twice, 113k, no edit) and the 100k cap arrives before a patch does. The
model's strength shows in the other direction: 9.3 steps per task against
Haiku's 15.6 under the structured runtime, tokens 20k lower per task at the
paired median, first edit in a gold file 32 / 32, no regressions, no wrong
patches. Two tasks are now unsolved by every arm: rich_006 (0 / 6 runs)
and, nearly, jinja_008 (1 / 6, Haiku `none` once). Both are two-site
propagation mutations in a long file — rich_006 drops `column.no_wrap` at
lines 558 and 824 of the 1 005-line `table.py`, jinja_008 drops
`dump_local_context` at lines 1074 and 1107 of the 1 998-line `compiler.py`
— and an absence cannot be found by searching for its name: every run reads
the file in 10–200-line slices and searches the propagated name (Sonnet:
`no_wrap` five times in a row) until the cap. One run of six edited rich_006's first site, none its
second; jinja_008's two sites were reached by three of the four Haiku runs
(one pass, two failures after editing) and by neither luna nor Sonnet, which
both ran out of tokens without an edit. Both tasks pass the gold oracle, so
they are hard rather than broken; v1.1 should decide whether a task whose
difficulty is "find where a name is *missing* in 1 000 lines within 100k
tokens" measures debugging or context handling, and either keep it with
that label or raise the cap for the long-file tasks.

Taxonomy verdict (§9.6 rule), five rows, 100 failed runs: localization 8,
wrong patch 9, regression 4, budget 79. Most failures reach the gold file and
run out of tokens there, so on v1-new the next lever is context handling
under the cap (compaction, read granularity, repeated-call control — the
things the structured runtime already does for Haiku and the baseline does
not do for anyone), then patch quality for Haiku; retrieval is not on the
list, and the `evidence` row says so twice. The Haiku baseline row is the
verdict's clearest case: 41 of its 46 failures are the cap, 36 of them
before a single edit.

Follow-up worth pre-registering now (not in §9.5): a Haiku *baseline* run on
v1-new, so the runtime is compared with its own model rather than with luna
(luna baseline 86 % vs Haiku structured 71 % mixes model and runtime). On
v0 the two Haiku configurations tied on success (26 / 28 each) with the
compact runtime at 38 % of the baseline's tokens. Expectation for v1-new:
success within ± 5 pp of `none` (a tie); the baseline's median tokens ≥ 1.5×
and its `budget_tokens` share higher, because it has no compaction and no
runtime-owned tests. `uv run python -m evals.runner --solver baseline --model
$H --suite v1-new --max-run-cost 6`, archived as `baseline-v1-haiku45`; ≈ $4.
Amended before the run (2026-09-28, after the Sonnet row, before any Haiku
baseline result exists): two repeats and a $10 cap if affordable
(`--repeat 2 --max-run-cost 10`, archive `baseline-v1-haiku45-x2`), so the
success comparison carries the same noise as `none` × 2; the expectation is
unchanged, and the Sonnet row sharpens one part of it — a baseline whose
transcript grows every step reaches the 100k cap in 11–14 steps on the
large-file tasks, and Haiku takes more steps than Sonnet, so the
`budget_tokens` share is expected well above `none`'s 33 %.

Haiku baseline × 2, read on 2026-09-28. The v0 tie did not transfer:
26 / 72 = 36.1 % against 51 / 72 for the same model under the same cap,
−34.7 pp where ± 5 pp was expected; the two token predictions held (1.66×,
97 % capped). The mechanism is the one the Sonnet row showed, at Haiku's
step count: the baseline sends the whole transcript on every call, 6.3k
input tokens per call against 4.4k for the compact runtime (+44 %), takes
the same number of steps (16.5 against 15.6), and so reaches the 100k cap
four or five working steps sooner — first edit at step 13 (median) against
11, and 40 of the 72 runs never edit at all (`none`: 4). The token
distribution is the cap itself: every run between 87.8k and 114.6k, pass
median 106k = fail median 107k. Where the baseline does reach an edit it
edits the right file (32 / 32), which is why the taxonomy calls 41 of its 46
failures budget and only 4 localization (jinja_002 twice, rich_006, rich_007
once each — never opened the gold file). Eight tasks pass both repeats
(click_003, click_004, click_007, jinja_004, jinja_006, rich_009,
sqlparse_005, sqlparse_007), 18 fail both, 10 flip; against `none` the
baseline is better on four tasks (click_003, jinja_001, jinja_006,
sqlparse_007 — one run each, inside the flip rate) and worse on 22.

What the number means, and what it does not. It is the same-model,
same-cap comparison the README can state: on v1-new the structured compact
runtime (compaction, runtime-owned tests, loop and edit control) turns
Haiku's 26 / 72 into 51 / 72 at 60 % of the tokens (64k against 107k at the
median) and $6.04 against $8.22. It is not evidence that the runtime makes
Haiku reason better: the baseline's failures are runs cut off mid-work, and
on v0 Haiku's three baseline failures all passed once the cap was raised to
300k (Phase 1). Sonnet is the other side of the same fact — it needs 9 steps
per task, fits inside 100k without help, and reaches 86 % as a baseline.
The right sentence for the README is "under a fixed 100k budget", not
"the runtime doubles success".

Optional follow-up, pre-registered here, Marco's call (not needed for the
README): the Haiku baseline with the cap lifted — `uv run python -m
evals.runner --solver baseline --model $H --suite v1-new --max-tokens
300000 --max-steps 60 --max-tool-calls 80 --max-cost 1.0 --max-run-cost 14`
(one repeat, ≈ $10–12, ~1 h), archived as `baseline-v1-haiku45-300k`.
Expectation: success 50–70 % (between the capped 36 % and the runtime's
71 %), median tokens ≥ 150k, budget_tokens share below 30 %. Readings fixed
in advance: ≥ 71 % → the runtime's contribution on v1 is budget discipline
alone, reported as "the same success at ≤ 40 % of the tokens"; ≤ 50 % →
the structure contributes beyond the budget (repeated calls with no loop
detector, tests the model forgets to run), and the runtime's success
advantage is real at any cap; in between → both, in the proportion the
number says.
