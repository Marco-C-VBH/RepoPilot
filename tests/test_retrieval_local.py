"""The local embedding model behind the dense channel (``fastembed``, bge-small).

Skipped unless the optional dependency is installed (``uv sync --extra
retrieval``); the first run downloads the model (~130 MB).  This checks the
seam, not the model's judgement: vectors have the right shape, and a
description of what ``put`` does ranks ``put`` above its sibling ``get``.
(Absolute claims -- "the method is the top hit" -- do not hold on a toy file:
bge-small ranked the one-line module stub first once, because for a chunk that
short the title's path tokens are most of the text; issue #10.)  What the
channel is worth on the benchmark is the offline evaluation's job.
"""

from __future__ import annotations

import pytest

from repopilot.retrieval.chunks import chunk_module
from repopilot.retrieval.dense import DenseIndex, LocalEmbedder
from tests.fakes import DEMO_FILES

pytestmark = pytest.mark.embeddings


def test_local_embedder_is_wired_and_orders_siblings_by_meaning() -> None:
    pytest.importorskip("fastembed")
    embedder = LocalEmbedder()
    vectors = embedder.embed_passages(["def put(self, key, value): ...", "hello world"])
    assert len(vectors) == 2 and len(vectors[0]) == 384
    assert abs(sum(x * x for x in vectors[0]) - 1.0) < 1e-4
    assert embedder.embed_query("x") == embedder.embed_query("x")

    chunks = chunk_module("demo/cache.py", DEMO_FILES["demo/cache.py"])
    index = DenseIndex(chunks, embedder)
    ranked = index.search("evicting one entry too late when the cache is full", n=len(chunks))
    order = [chunks[i].symbol or "(module)" for i, _ in ranked]
    assert order.index("Cache.put") < order.index("Cache.get"), order
