"""Repository indexing and retrieval (spec §7): chunks, BM25, embeddings,
symbols, fusion."""

from repopilot.retrieval.chunks import Chunk, chunk_module, chunk_workspace
from repopilot.retrieval.dense import HashingEmbedder, LocalEmbedder
from repopilot.retrieval.index import (
    CHANNELS,
    DEFAULT_RETRIEVAL,
    EMBEDDERS,
    RETRIEVAL_MODES,
    RepoIndex,
    RetrievalConfig,
    Retrieved,
    format_results,
    make_embedder,
)

__all__ = [
    "CHANNELS",
    "DEFAULT_RETRIEVAL",
    "EMBEDDERS",
    "RETRIEVAL_MODES",
    "Chunk",
    "HashingEmbedder",
    "LocalEmbedder",
    "RepoIndex",
    "RetrievalConfig",
    "Retrieved",
    "chunk_module",
    "chunk_workspace",
    "format_results",
    "make_embedder",
]
