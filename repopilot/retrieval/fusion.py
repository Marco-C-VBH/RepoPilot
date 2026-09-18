"""Reciprocal rank fusion (spec §7.3): ``sum(1 / (k + rank))`` over the channels."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

RRF_K = 60


@dataclass(frozen=True)
class Fused:
    index: int  # chunk index
    score: float
    ranks: dict[str, int] = field(default_factory=dict)  # channel -> 1-based rank


def rrf(rankings: Mapping[str, Sequence[int]], *, k: int = RRF_K) -> list[Fused]:
    """Fuse per-channel rankings of chunk indices; ties go to the lower index."""
    scores: dict[int, float] = defaultdict(float)
    ranks: dict[int, dict[str, int]] = defaultdict(dict)
    for channel, order in rankings.items():
        for position, index in enumerate(order, 1):
            scores[index] += 1.0 / (k + position)
            ranks[index][channel] = position
    fused = [Fused(index, score, dict(ranks[index])) for index, score in scores.items()]
    fused.sort(key=lambda f: (-f.score, f.index))
    return fused
