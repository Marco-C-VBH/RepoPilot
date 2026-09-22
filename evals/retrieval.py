"""Offline retrieval evaluation (spec §11.1, §12.1): no model, no Docker.

For every task the workspace is created at the buggy commit, the index is
built once with every channel, and each configuration ranks the chunks for
the bug report.  A chunk is relevant at file level when its path is one of the
task's ``gold_files`` and at symbol level when it is (or contains) one of its
``gold_symbols``.  Reported per configuration: Recall@5, Recall@10 and MRR at
both levels, query latency, and the index's build time and embedding count.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from repopilot.retrieval.dense import Embedder
from repopilot.retrieval.index import CHANNELS, RepoIndex
from repopilot.retrieval.metrics import (
    file_relevant,
    recall_at,
    reciprocal_rank,
    summarize,
    symbol_relevant,
)
from repopilot.sandbox.repo import DEFAULT_CACHE_DIR
from repopilot.tools.workspace import Workspace

CONFIGS: dict[str, tuple[str, ...]] = {
    "bm25": ("bm25",),
    "dense": ("dense",),
    "symbol": ("symbol",),
    "bm25+dense": ("bm25", "dense"),
    "bm25+dense+symbol": CHANNELS,
}
METRIC_KEYS = (
    "recall5_file",
    "recall10_file",
    "mrr_file",
    "recall5_symbol",
    "recall10_symbol",
    "mrr_symbol",
    "query_ms",
)


class TaskLike(Protocol):
    id: str
    repo: str
    base_commit: str
    bug_patch: str
    description: str
    gold_files: list[str]
    gold_symbols: list[str]


@dataclass
class RetrievalRow:
    task_id: str
    config: str
    channels: tuple[str, ...]
    recall5_file: float
    recall10_file: float
    mrr_file: float
    recall5_symbol: float
    recall10_symbol: float
    mrr_symbol: float
    query_ms: float
    first_file_rank: int | None
    first_symbol_rank: int | None
    top: list[dict[str, Any]] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        return {**self.__dict__, "channels": list(self.channels)}


def evaluate_workspace(
    task: TaskLike,
    workspace: Workspace,
    embedder: Embedder,
    *,
    configs: dict[str, tuple[str, ...]] = CONFIGS,
    k: int = 10,
    cache_dir: Path | None = DEFAULT_CACHE_DIR,
) -> tuple[list[RetrievalRow], dict[str, Any]]:
    """Rank the workspace's chunks for the task's report under each configuration."""
    index = RepoIndex(workspace, channels=CHANNELS, embedder=embedder, cache_dir=cache_dir)
    built = len(index.chunks)  # build now, so the build time is not charged to the first query
    rows: list[RetrievalRow] = []
    for name, channels in configs.items():
        started = time.perf_counter()
        results = index.search(task.description, k=k, channels=channels)
        query_ms = (time.perf_counter() - started) * 1000
        files = [file_relevant(r.chunk, task.gold_files) for r in results]
        symbols = [symbol_relevant(r.chunk, task.gold_symbols) for r in results]
        rows.append(
            RetrievalRow(
                task_id=task.id,
                config=name,
                channels=channels,
                recall5_file=recall_at(files, 5),
                recall10_file=recall_at(files, 10),
                mrr_file=round(reciprocal_rank(files), 4),
                recall5_symbol=recall_at(symbols, 5),
                recall10_symbol=recall_at(symbols, 10),
                mrr_symbol=round(reciprocal_rank(symbols), 4),
                query_ms=round(query_ms, 2),
                first_file_rank=next((i for i, hit in enumerate(files, 1) if hit), None),
                first_symbol_rank=next((i for i, hit in enumerate(symbols, 1) if hit), None),
                top=[r.to_record() for r in results],
            )
        )
    record = index.to_record()
    record["chunks"] = built
    return rows, record


def evaluate_tasks(
    tasks: Sequence[TaskLike],
    embedder: Embedder,
    *,
    configs: dict[str, tuple[str, ...]] = CONFIGS,
    k: int = 10,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    out_dir: Path | None = None,
    log: Any = print,
) -> dict[str, Any]:
    """Run ``evaluate_workspace`` over the tasks; write rows and the summary."""
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    t0 = time.monotonic()
    rows: list[RetrievalRow] = []
    indexes: dict[str, dict[str, Any]] = {}
    for task in tasks:
        with Workspace.create(
            task.repo, task.base_commit, task.bug_patch, cache_dir=cache_dir
        ) as workspace:
            task_rows, index_record = evaluate_workspace(
                task, workspace, embedder, configs=configs, k=k, cache_dir=cache_dir
            )
        rows.extend(task_rows)
        indexes[task.id] = index_record
        best = max(task_rows, key=lambda r: (r.recall10_file, r.mrr_file))
        log(
            f"{task.id:<18} chunks {index_record['chunks']:>4} · build "
            f"{index_record['build_seconds']:.2f}s · "
            + " · ".join(
                f"{r.config} R@10 {r.recall10_file:.0f}/{r.recall10_symbol:.0f}" for r in task_rows
            )
            + f" · best {best.config}"
        )
    flags = {t.id: t.flags() for t in tasks if hasattr(t, "flags")}
    summary = summarize_rows(rows, configs, flags=flags)
    summary.update(
        {
            "embedder": embedder.name,
            "k": k,
            "tasks": len(tasks),
            "started_at": started_at,
            "duration_seconds": round(time.monotonic() - t0, 1),
            "indexes": indexes,
            "index_build_seconds_total": round(
                sum(i["build_seconds"] for i in indexes.values()), 2
            ),
            "chunks_total": sum(i["chunks"] for i in indexes.values()),
            "embedded_total": sum(i["embedded"] for i in indexes.values()),
        }
    )
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        with (out_dir / "results.jsonl").open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row.to_record()) + "\n")
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        summary["results_dir"] = str(out_dir)
    return summary


GROUP_KEYS = ("suite", "report_level", "cross_module", "hidden_only")


def summarize_rows(
    rows: Sequence[RetrievalRow],
    configs: dict[str, tuple[str, ...]] = CONFIGS,
    *,
    flags: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Per-configuration means, and (given task ``flags``) the same per suite,
    report tier, cross-module and hidden-only bucket (docs/bench-v1-design.md §9.4)."""
    by_config: dict[str, Any] = {}
    for name in configs:
        subset = [r.to_record() for r in rows if r.config == name]
        if not subset:
            continue
        stats = summarize(subset, METRIC_KEYS)
        stats["query_ms_p50"] = round(statistics.median(r["query_ms"] for r in subset), 2)
        stats["tasks"] = len(subset)
        stats["misses_file"] = sorted(r["task_id"] for r in subset if not r["recall10_file"])
        stats["misses_symbol"] = sorted(r["task_id"] for r in subset if not r["recall10_symbol"])
        by_config[name] = stats
    summary: dict[str, Any] = {"kind": "retrieval", "configs": by_config}
    if flags:
        groups: dict[str, Any] = {}
        for key in GROUP_KEYS:
            values = sorted({str(f.get(key)) for f in flags.values() if key in f})
            if len(values) < 2:
                continue
            groups[key] = {}
            for value in values:
                ids = {tid for tid, f in flags.items() if str(f.get(key)) == value}
                per_config: dict[str, Any] = {}
                for name in configs:
                    subset = [r.to_record() for r in rows if r.config == name and r.task_id in ids]
                    if subset:
                        stats = summarize(subset, ("recall5_file", "recall10_file", "mrr_file"))
                        stats["tasks"] = len(subset)
                        per_config[name] = stats
                groups[key][value] = per_config
        if groups:
            summary["by_group"] = groups
    return summary


def format_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"{'configuration':<20} {'R@5 file':>8} {'R@10 file':>9} {'MRR file':>8} "
        f"{'R@5 sym':>7} {'R@10 sym':>8} {'MRR sym':>7} {'query p50':>9}"
    ]
    for name, stats in summary["configs"].items():
        lines.append(
            f"{name:<20} {stats['recall5_file']:>8.2f} {stats['recall10_file']:>9.2f} "
            f"{stats['mrr_file']:>8.2f} {stats['recall5_symbol']:>7.2f} "
            f"{stats['recall10_symbol']:>8.2f} {stats['mrr_symbol']:>7.2f} "
            f"{stats['query_ms_p50']:>7.1f}ms"
        )
        if stats["misses_file"]:
            lines.append(f"{'':<20} file misses: {', '.join(stats['misses_file'])}")
    for key, values in (summary.get("by_group") or {}).items():
        lines.append(f"by {key}:")
        for value, per_config in values.items():
            parts = [
                f"{name} R@10 {st['recall10_file']:.2f} MRR {st['mrr_file']:.2f}"
                for name, st in per_config.items()
            ]
            n = next((st["tasks"] for st in per_config.values()), 0)
            lines.append(f"  {value:<14} ({n:>2} task(s)) " + " · ".join(parts))
    lines.append(
        f"embedder {summary.get('embedder')} · {summary.get('chunks_total')} chunks over "
        f"{summary.get('tasks')} task(s) · index build {summary.get('index_build_seconds_total')}s "
        f"total · {summary.get('embedded_total')} chunks embedded now (the rest cached)"
    )
    return "\n".join(lines)
