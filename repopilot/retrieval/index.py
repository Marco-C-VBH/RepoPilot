"""The repository index: chunks, three channels, fusion (spec §7).

``RepoIndex`` is built from a ``Workspace`` and rebuilt lazily when the
workspace version changes (an edit, a reset), like the symbol index behind
``search_symbol``.  ``search`` returns fused results with each chunk's rank in
every channel that ranked it, so a trace can say why a chunk was shown.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repopilot.retrieval.bm25 import BM25Index
from repopilot.retrieval.chunks import Chunk, chunk_workspace
from repopilot.retrieval.dense import (
    DenseIndex,
    Embedder,
    EmbeddingCache,
    HashingEmbedder,
    LocalEmbedder,
)
from repopilot.retrieval.fusion import rrf
from repopilot.retrieval.lexer import tokens
from repopilot.retrieval.symbols import SymbolChannel
from repopilot.sandbox.repo import DEFAULT_CACHE_DIR
from repopilot.tools.workspace import Workspace

CHANNELS = ("bm25", "dense", "symbol")
RETRIEVAL_MODES = ("none", "tool", "evidence")
EMBEDDERS = ("local", "hash")


@dataclass(frozen=True)
class RetrievalConfig:
    """How the structured agent uses retrieval (design §3)."""

    mode: str = "none"  # none | tool | evidence
    channels: tuple[str, ...] = CHANNELS
    embedder: str = "local"  # local (fastembed, bge-small) | hash (dependency-free stand-in)
    k: int = 8  # the tool's default top-k
    per_channel: int = 20  # candidates taken from each channel before fusion
    evidence_k: int = 5  # chunks shown at PLAN in evidence mode
    evidence_chars: int = 4000  # the evidence block's budget
    snippet_lines: int = 30  # chunk text shown per result

    def __post_init__(self) -> None:
        if self.mode not in RETRIEVAL_MODES:
            raise ValueError(f"retrieval mode must be one of {RETRIEVAL_MODES}, not {self.mode!r}")
        unknown = [c for c in self.channels if c not in CHANNELS]
        if unknown or not self.channels:
            raise ValueError(f"retrieval channels must be among {CHANNELS}, not {self.channels!r}")
        if self.embedder not in EMBEDDERS:
            raise ValueError(f"embedder must be one of {EMBEDDERS}, not {self.embedder!r}")

    @property
    def enabled(self) -> bool:
        return self.mode != "none"

    @property
    def evidence(self) -> bool:
        return self.mode == "evidence"

    def to_record(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "channels": list(self.channels),
            "embedder": self.embedder,
            "k": self.k,
            "per_channel": self.per_channel,
            "evidence_k": self.evidence_k,
            "evidence_chars": self.evidence_chars,
        }


DEFAULT_RETRIEVAL = RetrievalConfig()


def make_embedder(name: str) -> Embedder:
    if name == "hash":
        return HashingEmbedder()
    if name == "local":
        return LocalEmbedder()
    raise ValueError(f"unknown embedder {name!r}; expected one of {EMBEDDERS}")


@dataclass(frozen=True)
class Retrieved:
    chunk: Chunk
    score: float
    ranks: dict[str, int] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {**self.chunk.to_record(), "score": round(self.score, 5), "ranks": self.ranks}


class RepoIndex:
    """Chunks + channels for one workspace; ``search`` fuses them."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        channels: Sequence[str] = CHANNELS,
        embedder: Embedder | None = None,
        per_channel: int = 20,
        cache_dir: Path | None = DEFAULT_CACHE_DIR,
    ) -> None:
        unknown = [c for c in channels if c not in CHANNELS]
        if unknown:
            raise ValueError(f"unknown retrieval channels {unknown}; expected {CHANNELS}")
        self.workspace = workspace
        self.channels = tuple(channels)
        self.embedder = embedder if embedder is not None else HashingEmbedder()
        self.per_channel = per_channel
        self.cache = EmbeddingCache(
            Path(cache_dir) / "embeddings" if cache_dir is not None else None, self.embedder.name
        )
        self._version: int | None = None
        self._chunks: list[Chunk] = []
        self._bm25: BM25Index | None = None
        self._dense: DenseIndex | None = None
        self._symbols: SymbolChannel | None = None
        self.build_seconds = 0.0
        self.embedded = 0
        self.searches = 0

    # -- build ------------------------------------------------------------------------
    @property
    def chunks(self) -> list[Chunk]:
        self._ensure()
        return self._chunks

    def _ensure(self) -> None:
        if self._version == self.workspace.version:
            return
        started = time.perf_counter()
        self._chunks = chunk_workspace(self.workspace)
        self._bm25 = (
            BM25Index([tokens(chunk.title + "\n" + chunk.text) for chunk in self._chunks])
            if "bm25" in self.channels
            else None
        )
        self._symbols = SymbolChannel(self._chunks) if "symbol" in self.channels else None
        if "dense" in self.channels:
            self._dense = DenseIndex(self._chunks, self.embedder, self.cache)
            self.embedded += self._dense.embedded
        else:
            self._dense = None
        self._version = self.workspace.version
        self.build_seconds = time.perf_counter() - started

    # -- search -----------------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        k: int = 8,
        channels: Sequence[str] | None = None,
        include_tests: bool = False,
    ) -> list[Retrieved]:
        """The fused top-``k`` chunks for ``query`` from the selected channels."""
        self._ensure()
        self.searches += 1
        wanted = tuple(channels) if channels else self.channels
        chunks = self._chunks
        allowed = {i for i, c in enumerate(chunks) if include_tests or not c.is_test}
        n = self.per_channel
        rankings: dict[str, list[int]] = {}
        if "bm25" in wanted and self._bm25 is not None:
            rankings["bm25"] = [i for i, _ in self._bm25.search(query, n * 2) if i in allowed][:n]
        if "dense" in wanted and self._dense is not None:
            rankings["dense"] = [i for i, _ in self._dense.search(query, n * 2) if i in allowed][:n]
        if "symbol" in wanted and self._symbols is not None:
            rankings["symbol"] = [i for i, _ in self._symbols.search(query, n * 2) if i in allowed][
                :n
            ]
        fused = rrf(rankings)
        return [Retrieved(chunks[f.index], f.score, f.ranks) for f in fused[:k]]

    def to_record(self) -> dict[str, Any]:
        self._ensure()
        return {
            "chunks": len(self._chunks),
            "channels": list(self.channels),
            "embedder": self.embedder.name,
            "build_seconds": round(self.build_seconds, 3),
            "embedded": self.embedded,
            "searches": self.searches,
        }


# ------------------------------------------------------------------- rendering


def snippet(chunk: Chunk, lines: int) -> str:
    """The first ``lines`` lines of a chunk with their line numbers."""
    numbered = chunk.numbered()
    shown = [f"{number:>5}| {line}" for number, line in numbered[:lines]]
    if len(numbered) > lines:
        shown.append(f"      ... ({chunk.lines} lines; read_file for the rest)")
    return "\n".join(shown)


def format_results(results: Sequence[Retrieved], *, snippet_lines: int, budget: int) -> str:
    """Tool output / evidence block: one header line per chunk, then its snippet."""
    if not results:
        return "no matching code found"
    parts: list[str] = []
    used = 0
    for rank, item in enumerate(results, 1):
        chunk = item.chunk
        ranks = ", ".join(f"{c} #{r}" for c, r in sorted(item.ranks.items()))
        header = (
            f"[{rank}] {chunk.path}:{chunk.start_line}-{chunk.end_line}  "
            f"{chunk.symbol_type}  {chunk.symbol or '(module)'}"
            + ("  [test]" if chunk.is_test else "")
            + f"  ({ranks})"
        )
        body = snippet(chunk, snippet_lines)
        block = header + "\n" + body
        if used + len(block) > budget and parts:
            parts.append(f"... {len(results) - rank + 1} more result(s) not shown (budget)")
            break
        parts.append(block)
        used += len(block) + 1
    return "\n\n".join(parts)
