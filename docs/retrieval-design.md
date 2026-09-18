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
