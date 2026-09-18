"""The semantic channel: embeddings and cosine search (spec §7.2).

``Embedder`` is the seam.  ``LocalEmbedder`` runs ``BAAI/bge-small-en-v1.5``
through ``fastembed`` (ONNX on CPU: no key, no torch, reproducible offline);
``HashingEmbedder`` is a deterministic bag-of-sub-tokens projection with no
dependency, for tests and CI.  Vectors are unit-normed and compared in plain
Python -- a repository here has a few hundred chunks, and the embedding, not
the dot product, is the cost.  ``EmbeddingCache`` keeps vectors per model by
chunk-text hash, so a second run of a task embeds nothing and an edit
re-embeds only the chunks it changed.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from repopilot.retrieval.chunks import Chunk
from repopilot.retrieval.lexer import tokens

LOCAL_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_TEXT_CHARS = 1500  # bge-small reads 512 tokens; longer chunk text is cut
HASH_DIM = 64


class Embedder(Protocol):
    name: str

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Feature hashing of code tokens into ``dim`` buckets with signs; unit-normed.

    Deterministic and dependency-free.  Lexical overlap is all it can see, which
    is enough to test the channel, the fusion and the cache end to end.
    """

    def __init__(self, dim: int = HASH_DIM) -> None:
        self.dim = dim
        self.name = f"hash-{dim}"

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            bucket = value % self.dim
            vector[bucket] += 1.0 if (value >> 32) & 1 else -1.0
        return normalize(vector)

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class LocalEmbedder:
    """``fastembed`` sentence embeddings; the model is loaded on first use."""

    def __init__(self, model: str = LOCAL_MODEL) -> None:
        self.model = model
        self.name = model.replace("/", "--")
        self._engine: Any = None

    def _load(self) -> Any:
        if self._engine is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise RuntimeError(
                    "the local embedder needs the optional dependency: uv sync --extra retrieval "
                    "(or pass --embedder hash for a dependency-free stand-in)"
                ) from exc
            self._engine = TextEmbedding(model_name=self.model)
        return self._engine

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        engine = self._load()
        return [normalize([float(x) for x in vector]) for vector in engine.passage_embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        engine = self._load()
        vector = next(iter(engine.query_embed(text)))
        return normalize([float(x) for x in vector])


def normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector


def embedding_text(chunk: Chunk) -> str:
    """What gets embedded: the chunk's title, then its text, clipped."""
    return (chunk.title + "\n" + chunk.text)[:EMBED_TEXT_CHARS]


class EmbeddingCache:
    """Vectors by chunk-text hash: one JSON file per embedder under ``directory``,
    or memory only when ``directory`` is None (a rebuild after an edit still
    re-embeds only the chunks whose text changed)."""

    def __init__(self, directory: Path | None, embedder_name: str) -> None:
        self.path = Path(directory) / f"{embedder_name}.json" if directory is not None else None
        self._vectors: dict[str, list[float]] | None = None
        self._dirty = False

    @property
    def vectors(self) -> dict[str, list[float]]:
        if self._vectors is None:
            self._vectors = {}
            if self.path is not None and self.path.exists():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    self._vectors = {k: list(map(float, v)) for k, v in data.items()}
                except (OSError, ValueError):
                    self._vectors = {}
        return self._vectors

    def get(self, key: str) -> list[float] | None:
        return self.vectors.get(key)

    def put(self, key: str, vector: list[float]) -> None:
        self.vectors[key] = vector
        self._dirty = True

    def save(self) -> None:
        if not self._dirty or self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({k: [round(x, 6) for x in v] for k, v in self.vectors.items()}),
            encoding="utf-8",
        )
        tmp.replace(self.path)
        self._dirty = False


class DenseIndex:
    """Unit vectors for every chunk; cosine similarity search."""

    def __init__(
        self,
        chunks: Sequence[Chunk],
        embedder: Embedder,
        cache: EmbeddingCache | None = None,
    ) -> None:
        self.embedder = embedder
        self.embedded = 0  # chunks embedded now (the rest came from the cache)
        keys = [chunk.text_hash() for chunk in chunks]
        vectors: list[list[float] | None] = [cache.get(k) if cache else None for k in keys]
        missing = [i for i, v in enumerate(vectors) if v is None]
        if missing:
            fresh = embedder.embed_passages([embedding_text(chunks[i]) for i in missing])
            for i, vector in zip(missing, fresh, strict=True):
                vectors[i] = vector
                if cache is not None:
                    cache.put(keys[i], vector)
            self.embedded = len(missing)
            if cache is not None:
                cache.save()
        self.vectors: list[list[float]] = [v for v in vectors if v is not None]

    def search(self, query: str, n: int = 20) -> list[tuple[int, float]]:
        if not self.vectors:
            return []
        q = self.embedder.embed_query(query)
        scores = [
            (index, sum(a * b for a, b in zip(q, vector, strict=True)))
            for index, vector in enumerate(self.vectors)
        ]
        scores.sort(key=lambda pair: (-pair[1], pair[0]))
        return [(i, s) for i, s in scores[:n] if s > 0]
