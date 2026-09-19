# Phase 3 — retrieval

Written 2026-09-18, before any retrieval code ran on the benchmark. Design and
pre-registered experiment, as for Phase 2 (`docs/runtime-design.md`): the
expectations were written down first so the results can be read against them.

## 1. What failure mode, what metric (spec §19)

The spec's Phase 3 is three retrieval channels over semantic code chunks (§7)
and the retrieval ablation (§12.1: Recall@10, task success, latency per
configuration). Before designing it, what the benchmark can measure:

- The leak audit (`docs/leak-audit.md`) established that **localization is
  free on Bench v0**: the first `edit_file` of every run touched a gold file
  (71 / 71), `search_symbol` on a name from the report lands on the right
  function in one call, and even the two hidden-only tasks name the function.
  A retrieval configuration cannot raise success on v0, because no run fails
  for want of the right file. That is a *prediction* of this design: success
  stays at 40 / 42 (Haiku, compact) whatever retrieval does.
- What v0 can measure is the **cost of localization**. In the final Phase 2
  experiment the LOCALIZE phase took 253 of Haiku's 456 compact steps (301 of
  470 with the full history) — more than half of every run is the model
  searching and reading before it states a hypothesis. Evidence handed to the
  model at PLAN should shorten that phase; that is the agent-level metric.
- What retrieval *quality* means here is measured offline, without a model:
  the bug report as the query, the task's `gold_files` / `gold_symbols` as
  the relevant set, Recall@5 / Recall@10 / MRR per channel and per fusion
  (spec §11.1). This is cheap (no Docker, no API), runs in seconds, and is the
  ablation §12.1 asks for — on v0 it measures how well each channel reads a
  report that names its target; on Bench v1 (identifiers stripped, symptom
  and cause in different modules) it will measure retrieval proper.

| failure mode | retrieval response | metric that must move |
|---|---|---|
| LOCALIZE dominates the run (Haiku: 6 of 11 steps per task) | top-k evidence for the report at PLAN; a `retrieve` tool in LOCALIZE / PATCH | LOCALIZE steps per task, tool calls per task, tokens per task |
| reports phrased in prose, not identifiers (Bench v1) | dense channel + fusion | Recall@10 on v1 (later) |
| the wrong first hypothesis (`cachetools_001/003`) | not addressed: evidence points at the same functions the model already finds | — (reported) |

## 2. Index

`repopilot/retrieval/`: built from a `Workspace` (the buggy tree), rebuilt
lazily when the workspace version changes (edits, resets), like the symbol
index.

**Chunks** (`chunks.py`, Python `ast` — Marco's decision; the benchmark is
Python-only and the symbol table already comes from `ast`; Tree-sitter is a
swap behind the same `Chunk` if another language ever appears): one chunk
per function, method, class and module, non-overlapping —

- *function / method*: the `def` with its body (decorators included);
- *class*: the class header, docstring and class-level statements, plus one
  line per method signature (the bodies are their own chunks);
- *module*: docstring, imports and top-level statements outside any `def` /
  `class`;
- chunks longer than 120 lines are split into windows that keep the symbol
  metadata (`part` 1..n).

Metadata per chunk (spec §7.1): `path`, `symbol` (qualified), `symbol_type`,
`start_line`, `end_line`, `imports` (names the module imports), `is_test`.
Test files are indexed but excluded by default from tool results and evidence
(`include_tests`): the bug is in the source, and the runtime already gives the
model the failing tests' names.

**Channels** (spec §7.2):

- *Lexical* (`bm25.py`): BM25 (k1 = 1.5, b = 0.75) over a code-aware
  tokenisation — identifiers split on `_` and CamelCase into sub-tokens,
  the whole identifier kept as a token too, lower-cased, no stemming; the
  chunk's path and symbol are tokens as well. No dependency.
- *Semantic* (`dense.py`): an `Embedder` protocol with two implementations —
  `LocalEmbedder` over `fastembed` (`BAAI/bge-small-en-v1.5`, 384 dimensions,
  ONNX on CPU, no key, no torch; Marco's decision: local, reproducible,
  free) and `HashingEmbedder` (deterministic bag-of-sub-tokens projected to
  64 dimensions; no dependency, for tests and CI). Vectors are unit-normed;
  cosine similarity in plain Python (a few hundred chunks per repository).
  Embeddings are cached per model by chunk-text hash under the repo cache, so
  an edit re-embeds only the chunks it changed and a second run of the same
  task embeds nothing. What is embedded is the chunk's title (path, kind,
  symbol) followed by its text; for a very short chunk the title dominates,
  and on the demo file that put a one-line module stub above the method the
  query described (issue #10) — whether it matters on the benchmark is what
  the offline evaluation measures.
- *Symbol* (`symbols.py`): identifiers extracted from the query (CamelCase
  and snake_case words, dotted names, back-ticked spans) matched against
  chunk symbols and against the names a chunk defines or imports — exact
  qualified-name matches first, then name matches, then substring matches.

**Fusion** (`fusion.py`): reciprocal rank fusion, `1 / (60 + rank)`, over
each channel's top 20 (spec §7.3). No reranker in this phase; it is the
"+ reranker" row of §12.1 and is justified only by a measured gain.

The trace records every retrieval (`retrieval` event): query, channels, the
top-k with each chunk's per-channel rank, latency. That is the data for
§12.1's latency column and for auditing what the model was shown.

## 3. How the agent uses it (Marco's decision: both, measured separately)

`--retrieval` on the structured solver:

- `none` — Phase 2 as measured (control).
- `tool` — a sixth model tool, `retrieve(query, k=8, include_tests=false)`:
  the fused top-k as `path:start-end  kind  symbol` with the chunk text
  (clipped), available in LOCALIZE and PATCH next to the existing tools.
  Nothing is pushed; the model decides whether to call it.
- `evidence` — the tool, plus the runtime retrieves the top 5 chunks for the
  bug report before PLAN and shows them as *retrieved evidence*: in the PLAN
  prompt with the full history; in the WORKING STATE during PLAN and LOCALIZE
  with the compact context (dropped from the state in PATCH and ANALYZE, so
  it costs nothing once the model is editing). Budget: 5 chunks, at most
  4,000 characters — a fraction of spec §8's 8–12k tokens, chosen because the
  compact arm's whole prompt is ~3.6k tokens per call and the evidence must
  not undo Phase 2b.

The baseline agent does not get retrieval: it is the Phase 1 control and the
architecture ablation's other arm.

## 4. Offline evaluation

`evals/retrieval.py` + `scripts/retrieval_eval.py`: for every task, create
the workspace at the buggy commit (host only), build the index, and for each
configuration rank the chunks for the report. A chunk is relevant at file
level when its path is in `gold_files`, at symbol level when its symbol is
(or contains) a `gold_symbol`. Reported per configuration: Recall@5,
Recall@10, MRR (file and symbol level), index build time, query latency.
Configurations: `bm25`, `dense`, `symbol`, `bm25+dense`, `bm25+dense+symbol`.
Results go to `results/retrieval-<timestamp>/` and are archived like agent
runs (`evals/experiments/retrieval-v0`).

## 5. Pre-registered expectations (Bench v0)

Offline, `LocalEmbedder`, query = the bug report verbatim:

| configuration | file-level Recall@10 | symbol-level Recall@10 |
|---|---|---|
| BM25 | ≥ 0.90 (reports name identifiers; the leak audit found them in every report) | 0.70–0.85 |
| dense (bge-small) | 0.60–0.80 (prose over code; a small model) | 0.45–0.65 |
| symbol | ≥ 0.85 on file level (an identifier in the report resolves to its definition); lower where the report names a class and the gold is one method | 0.60–0.80 |
| BM25 + dense | ≥ BM25 (RRF must not lose the lexical hit) | ≥ BM25 |
| all three | ≥ BM25 + dense; the best row | the best row |

Query latency under 50 ms per channel on these repositories; index build
under 2 s without embeddings and under 30 s with them on a laptop CPU, once
(then cached).

Agent level (Haiku, compact context, 2 × 14 per arm, same budget as Phase 2):

| | `none` (Phase 2 final) | expected `tool` | expected `evidence` |
|---|---|---|---|
| success | 40 / 42 | 26–27 / 28 (flat) | 26–27 / 28 (flat) |
| LOCALIZE steps per run | 6.0 | 5–6 (the model may not call it; reported) | ≤ 4 |
| tool calls per task | 8.5 | ≤ 8.5 | ≤ 6.5 |
| tokens per task (median) | 38.4k | ≤ 40k | ≤ 38k (evidence costs ~1k per PLAN/LOCALIZE step; fewer steps should pay for it) |
| `retrieve` calls per run | — | reported | reported |
| forced hypotheses | 10 / 42 | ≤ 10 | ≤ 5 (fewer 10-call LOCALIZE visits) |

If `evidence` does not cut LOCALIZE steps, the model is not reading the
evidence and the design is wrong for it; if it cuts steps but raises tokens,
the evidence budget is too large. Both are results.

## 6. Not in this phase

Reranking (§12.1's last row; needs a measured gap first). Re-querying after a
failing test (spec §8). Retrieval-based working memory (§12.3's third arm).
Tree-sitter. pgvector (the index is per task and lives in memory plus a
file cache; a database is Phase 5). Bench v1, where retrieval decides tasks —
the benchmark-extension stage.

## 7. Offline results (2026-09-18, `retrieval-v0`)

`scripts/retrieval_eval.py`, `LocalEmbedder`, k = 10, the 14 reports verbatim;
6,467 chunks over the 14 workspaces (413 per cachetools task, 522 tenacity,
448 toolz).

| configuration | R@5 file | R@10 file | MRR file | R@5 symbol | R@10 symbol | MRR symbol | query p50 | vs. §5 |
|---|---|---|---|---|---|---|---|---|
| BM25 | 0.93 | 0.93 | 0.86 | 0.79 | 0.93 | 0.54 | 1.7 ms | file ✓ (≥ 0.90); symbol above the 0.70–0.85 range |
| dense (bge-small) | 0.93 | **1.00** | 0.87 | **0.93** | **1.00** | 0.81 | 35.6 ms | far above 0.60–0.80 / 0.45–0.65: the best single channel |
| symbol | 0.86 | 0.93 | 0.65 | 0.71 | 0.93 | 0.48 | 0.5 ms | file ✓ (≥ 0.85); the weakest ranker (MRR) |
| BM25 + dense | 0.93 | 0.93 | 0.86 | 0.93 | 0.93 | 0.80 | 39.0 ms | ≥ BM25 ✓; < dense ✗ |
| all three | 0.93 | 0.93 | **0.93** | 0.93 | 0.93 | **0.89** | 42.6 ms | ≥ BM25 + dense ✓ on MRR; R@10 unchanged |

Latency held (every channel under 50 ms; the dense channel's 36 ms is the
query embedding). Index build did not: 56 s, 86 s and 100 s for the first
task of each repository — bge-small embeds these ~400-token chunks at about
6 per second on the laptop CPU, not the 30 s budgeted — and 0.14–0.38 s for
every later task of the same repository, because the cache is keyed by chunk
text and a task's bug changes one chunk (1,397 chunks embedded for 6,467
indexed). The agent runs inherit that cache.

Three things the table says.

*The dense channel was under-predicted.* It is the only configuration with
perfect recall at 10, at file and symbol level, and its MRR ties BM25. The
reports mix prose with identifiers; a small embedding model reads both, and
the crowding-out of functions by short module chunks feared in issue #10 did
not happen on real files (3 of the 137 dense top-10 slots are module chunks;
BM25 has 4).

*Fusion sharpens agreement and buries dissent.* All three channels fused
give the best first hit (MRR 0.93 / 0.89: the gold is rank 1 for 13 of 14
tasks at file level) but no better recall than BM25, because of one task.
`cachetools_005` reports that `@cached(...)` never stores anything; the gold
is `_wrapper` in the private module `src/cachetools/_cached.py`, and the
report names only the public surface (`cached`, `LRUCache`, `square.cache`).
BM25 and the symbol channel rank `lru_cache`, `_HashedTuple`, `LRUCache` and
the public `cached` function — the entry point, one hop from the bug — and
never the gold file. Dense alone finds it (`_locked` at rank 6, `_wrapper` at
rank 10). Reciprocal rank fusion sums `1 / (60 + rank)`, so a chunk two
channels place at rank 2 scores 2 / 62 and a chunk one channel places at
rank 6 scores 1 / 66: the channels' shared, wrong picture outvotes the one
right signal, and the fused top 10 has no gold at all. "Fusion ≥ BM25" held;
"fusion ≥ the best channel" did not, and §5 did not claim it. This is the
one v0 task shaped like the Bench v1 tasks (symptom in one module, cause in
another), and it is where retrieval quality actually differs by configuration.

*The symbol channel is a poor ranker on its own and a good tie-breaker.* Its
MRR is the lowest (0.65 file), because plain words in a report match many
definitions; added to BM25 + dense it lifts MRR from 0.86 to 0.93 — when the
report names the symbol, that vote breaks ties in the right direction.

Decisions from this. The agent runs go ahead as pre-registered, with all
three channels fused (the evidence for `cachetools_005` will show the public
`cached` function at rank 3 — the right place to start reading, even without
the gold). Nothing is tuned on the one miss: a reranker, a fusion that keeps
each channel's top hit, or a dense-weighted fusion are the §12.1 "+ reranker"
row's territory and need Bench v1's sample to be judged. The pre-registration
for v1 can now be sharper: dense recall will be the ceiling, BM25 and symbol
recall will fall with identifier-free reports, and fusion will lose to dense
alone on cross-module tasks unless it stops requiring agreement.

## 8. Agent-level results (2026-09-19, `structured-p3-tool-haiku45-x2`, `structured-p3-evidence-haiku45-x2`)

Haiku, compact context, 2 × 14 per arm, same budget as Phase 2; the control
is the Phase 2 final compact experiment (3 × 14). Per-run figures for the
phase counts, since the arms differ in size.

| | `none` (Phase 2 final) | `tool` | expected | `evidence` | expected |
|---|---|---|---|---|---|
| success | 40 / 42 | 27 / 28 | 26–27 / 28 ✓ | 27 / 28 | 26–27 / 28 ✓ |
| steps per task | 10.9 | 11.3 | — | 10.5 | — |
| LOCALIZE steps per run | 6.0 | 7.0 | 5–6 ✗ | 5.2 | ≤ 4 ✗ |
| PATCH steps per run | 3.5 | 3.0 | — | 4.0 | — |
| LOCALIZE tool calls per run | — | 6.8 | — | 4.3 | — |
| tool calls per task | 8.5 | 8.6 | ≤ 8.5 ≈ | 7.1 | ≤ 6.5 ✗, close |
| tokens per task (median) | 38.4k | 41.3k | ≤ 40k ✗ | 41.2k | ≤ 38k ✗ |
| cost per task (median) | $0.047 | $0.050 | — | $0.050 | — |
| `retrieve` calls | — | **0** in 28 runs | reported | **0** in 28 runs | reported |
| forced hypotheses per run | 0.24 | 0.29 | ≤ 0.24 ✗ | 0.14 | ≤ 0.12 ≈ |
| `budget_tokens` | 2 / 42 | 1 / 28 | — | 2 / 28 (one a PASS on the cap) | — |
| verified on a green run | 95.2% | 96.4% | — | 92.9% | — |
| invalid calls | 1.7% | 0.4% | — | 0.0% | — |
| edits (failed / whitespace-matched) | — | 32 (0 / 3) | — | 43 (1 / 6) | — |
| edit-cap refusals | — | 0 | — | 5 | — |

What the traces say, in order of weight.

*Haiku never called `retrieve`.* Zero calls in 56 runs, with the tool
defined in every LOCALIZE and PATCH prompt and described as the way to search
by description. Given `search_symbol` and `search_code`, which it has used
since Phase 1, the model does not reach for a third search tool it was not
told to prefer. The `tool` arm is therefore the compact control re-run with
one more tool definition in the prompt: 27 / 28 (the Phase 2 pattern: one
`cachetools_003` failure), 11.3 steps, 41.3k tokens against 38.1–38.8k in the
three earlier compact runs — within Haiku's run-to-run spread, plus ~150
tokens of schema per call. A retrieval tool the model may call is, for this
model, no retrieval at all; whatever retrieval does for the agent has to be
done by the runtime.

*Evidence at PLAN did what it was for, and the runtime took the saving
back.* The evidence block held the gold file at rank 1 in 26 of 28 runs (the
two misses are `cachetools_005`, §7); with it, LOCALIZE fell from 7.0 to 5.2
steps per run, LOCALIZE tool calls from 6.8 to 4.3, `search_symbol` calls
from 39 to 6 over the arm, forced hypotheses halved, and in several runs the
model stated its hypothesis at its first LOCALIZE turn with no tool call at
all ("I can see the bug clearly from the code provided"). PATCH then grew from
3.0 to 4.0 steps per run: 28 of the arm's 33 PATCH-phase `read_file` calls
re-read a range the evidence had shown, because the compact state dropped the
block at PATCH, exactly when `edit_file` needed the exact text (issue #12).
Tokens per task ended flat (41.2k against 41.3k): the block costs ~800 tokens
per PLAN and LOCALIZE call (PLAN prompts 2.3k → 3.6k, LOCALIZE 4.0k → 4.8k)
and the re-reads cost about what the shorter LOCALIZE saved. The design's
prediction was a lower token count *because* of fewer steps; the steps fell
where predicted and reappeared where the design had not looked.

*One run exposed a Phase 2a.1 bug.* `toolz_003` run 1 in the evidence arm
had the correct fix in the tree at step 13 and hit the token cap at step 25
(PASS on the cap, $0.123): every whitespace-tolerant edit re-indented the
inserted lines by the first line's offset, leaving `if key in d2:` at 13
spaces; the model saw it, tried to fix the indentation three times, and the
tool wrote the file's wrong indentation back each time; then five edits were
refused at the PATCH visit's cap while the model kept reading and retrying
instead of ending its turn (issue #11). All 13 whitespace-matched edits in
the last four runs left a mis-indented block; only this one turned into a
loop. Not a retrieval effect — the evidence arm merely produced the run that
made it visible.

*Per task.* Evidence helped most where localization had been expensive:
`cachetools_001` 12 / 14 steps → 8 / 7, `cachetools_005` 14 / 17 → 7 / 10
(the report's public entry point `cached` at evidence rank 3 was enough,
without the gold), `cachetools_003` 23 → 13 in the passing run, `toolz_001`
8 / 19 → 6 / 14. It cost steps on tenacity, whose reports already name the
function and whose LOCALIZE was short: `tenacity_001` 9 / 8 → 15 / 7 (a run
that read the file whole twice and hit the 10-call LOCALIZE cap despite the
evidence), `tenacity_004` 14 / 14 → 17 / 16. Success did not move (27 / 28
both, `cachetools_003.1` the failure in both) — §1's prediction.

## 9. Phase 3.1 (written before running): the same experiment with three fixes

Changes since §8, all in the runtime and tools, none in retrieval:

1. `_reindent` places inserted lines relative to the nearest kept line and
   honours a pure re-indentation (issue #11) — a shared tool, so the control
   moves too.
2. Once a PATCH visit's edits are spent and the tree changed, the runtime runs
   the tests instead of waiting for the model to end its turn (issue #11).
3. The evidence stays in the compact state while no patch is in place —
   PLAN, LOCALIZE and PATCH up to the first edit (issue #12).

Runs: `--retrieval none` × 2 (the control, re-run because of 1 and 2) and
`--retrieval evidence` × 2, Haiku, compact; archived as
`structured-p31-none-haiku45-x2` and `structured-p31-evidence-haiku45-x2`.
The `tool` arm is not re-run: 0 calls in 28 runs is the result.

| | expected `none` | expected `evidence` |
|---|---|---|
| success | 26–27 / 28 | 26–27 / 28, same failing task |
| LOCALIZE steps per run | 6–7 | ≤ 5.2 (as in §8) |
| PATCH steps per run | 3–3.5 | ≤ 3.5 (the re-reads gone: PATCH reads overlapping the evidence < 5 in 28 runs) |
| tool calls per task | 8–9 | ≤ 6.5 |
| tokens per task (median) | 37–41k | below `none` by ≥ 2k |
| whitespace-matched edits leaving a mis-indented block | 0 of N | 0 of N |
| edit-cap forced test runs | reported | reported |
| `budget_tokens` | ≤ 2 / 28 | ≤ 1 / 28 |

If `evidence` still does not beat `none` on tokens with the re-reads gone,
the evidence block's ~800 tokens per call cost more than the localization it
saves on v0, and the honest conclusion is that runtime-injected evidence is a
step saver, not a token saver, on a benchmark where localization is free.
