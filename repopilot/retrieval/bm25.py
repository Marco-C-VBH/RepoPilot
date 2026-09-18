"""The lexical channel: BM25 over code tokens (spec §7.2), no dependencies."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

from repopilot.retrieval.lexer import tokens


class BM25Index:
    """Okapi BM25 (k1 = 1.5, b = 0.75) over pre-tokenised documents."""

    def __init__(self, documents: Sequence[Sequence[str]], *, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.counts = [Counter(doc) for doc in documents]
        self.lengths = [len(doc) for doc in documents]
        self.avg_length = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        df: Counter[str] = Counter()
        for count in self.counts:
            df.update(count.keys())
        n = len(self.counts)
        self.idf = {
            term: math.log((n - freq + 0.5) / (freq + 0.5) + 1.0) for term, freq in df.items()
        }

    def search(self, query: str, n: int = 20) -> list[tuple[int, float]]:
        """``(document index, score)`` for the best ``n`` documents, best first."""
        query_terms = Counter(tokens(query))
        if not query_terms or not self.counts:
            return []
        scores: list[tuple[int, float]] = []
        for index, count in enumerate(self.counts):
            score = 0.0
            norm = self.k1 * (1 - self.b + self.b * self.lengths[index] / (self.avg_length or 1))
            for term, weight in query_terms.items():
                tf = count.get(term)
                if not tf:
                    continue
                score += self.idf[term] * (tf * (self.k1 + 1)) / (tf + norm) * min(weight, 3)
            if score > 0:
                scores.append((index, score))
        scores.sort(key=lambda pair: (-pair[1], pair[0]))
        return scores[:n]
