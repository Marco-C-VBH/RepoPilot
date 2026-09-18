"""Retrieval quality against a task's gold files and symbols (spec §11.1)."""

from __future__ import annotations

from collections.abc import Sequence

from repopilot.retrieval.chunks import Chunk


def file_relevant(chunk: Chunk, gold_files: Sequence[str]) -> bool:
    return chunk.path in gold_files


def symbol_relevant(chunk: Chunk, gold_symbols: Sequence[str]) -> bool:
    """The chunk *is* a gold symbol, or contains one (``Class`` for ``Class.method``,
    a function chunk for a nested name), or is the module chunk of a gold module."""
    if not chunk.symbol:
        return False
    for gold in gold_symbols:
        if chunk.symbol == gold or gold.startswith(chunk.symbol + "."):
            return True
        if chunk.symbol.endswith("." + gold) or chunk.symbol == gold.rsplit(".", 1)[-1]:
            return True
    return False


def recall_at(relevant: Sequence[bool], k: int) -> float:
    """1.0 when any of the first ``k`` results is relevant (one gold set per task)."""
    return 1.0 if any(relevant[:k]) else 0.0


def reciprocal_rank(relevant: Sequence[bool]) -> float:
    for position, hit in enumerate(relevant, 1):
        if hit:
            return 1.0 / position
    return 0.0


def summarize(rows: Sequence[dict[str, float]], keys: Sequence[str]) -> dict[str, float]:
    """Means of the given keys over per-task rows, rounded."""
    if not rows:
        return {key: 0.0 for key in keys}
    return {key: round(sum(float(r[key]) for r in rows) / len(rows), 4) for key in keys}
