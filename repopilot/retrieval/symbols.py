"""The symbol-aware channel (spec §7.2): identifiers in the query, resolved to
the chunks that define, contain or import them.

A bug report that says "``TTLCache.expire`` drops the wrong entry" points at
one method; a lexical search sees five tokens, this channel sees one name.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence

from repopilot.retrieval.chunks import Chunk

_BACKTICK = re.compile(r"`([^`\n]{1,80})`")
_DOTTED = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b")
_SNAKE = re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b|\b_+[a-z][a-z0-9_]*\b")
_CAMEL = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b|\b[A-Z][a-z0-9]{2,}\b")
_WORD = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")

_COMMON = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "when",
        "that",
        "this",
        "from",
        "into",
        "than",
        "then",
        "not",
        "but",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "does",
        "did",
        "should",
        "would",
        "could",
        "after",
        "before",
        "because",
        "instead",
        "while",
        "about",
        "between",
    }
)


def query_identifiers(query: str) -> list[str]:
    """Identifier-like spans of a query, most specific first, without duplicates.

    Back-ticked spans, dotted names (``Class.method``), snake_case and
    CamelCase words are identifiers on their face; plain words are kept too
    (lower priority) so that ``expire`` matches ``expire()`` -- they only count
    when a chunk actually defines a symbol of that name.
    """
    found: list[str] = []
    for pattern in (_BACKTICK, _DOTTED, _SNAKE, _CAMEL):
        for match in pattern.finditer(query):
            found.append(match.group(1) if pattern is _BACKTICK else match.group(0))
    for match in _WORD.finditer(query):
        found.append(match.group(0))
    seen: dict[str, None] = {}
    for item in found:
        item = item.strip().strip("()[]{}.,;:'\"")
        if item and item.lower() not in _COMMON:
            seen.setdefault(item, None)
    # A component of a dotted name (``Cache`` and ``put`` of ``Cache.put``) is not a
    # second identifier: the dotted name already names the method, and crediting the
    # class again would rank the container above the member.
    components = {part for item in seen if "." in item for part in item.split(".")}
    return [item for item in seen if "." in item or item not in components]


class SymbolChannel:
    """Ranks chunks by the query identifiers they define, contain or import."""

    def __init__(self, chunks: Sequence[Chunk]) -> None:
        self.chunks = list(chunks)
        self.by_qualname: dict[str, list[int]] = defaultdict(list)
        self.by_name: dict[str, list[int]] = defaultdict(list)
        self.by_defined: dict[str, list[int]] = defaultdict(list)
        self.by_import: dict[str, list[int]] = defaultdict(list)
        for index, chunk in enumerate(self.chunks):
            if chunk.symbol:
                self.by_qualname[chunk.symbol].append(index)
                self.by_name[chunk.name].append(index)
                # Class.method is also findable as "method" of the class chunk.
            for name in chunk.defines:
                self.by_defined[name].append(index)
            for name in chunk.imports:
                self.by_import[name].append(index)

    def search(self, query: str, n: int = 20) -> list[tuple[int, float]]:
        scores: dict[int, float] = defaultdict(float)
        for identifier in query_identifiers(query):
            plain = "." not in identifier and "_" not in identifier and identifier.islower()
            for index in self.by_qualname.get(identifier, ()):
                scores[index] += 5.0
            name = identifier.rsplit(".", 1)[-1]
            for index in self.by_name.get(name, ()):
                scores[index] += 3.0 if not plain else 2.0
            for index in self.by_defined.get(name, ()):
                scores[index] += 1.0 if not plain else 0.5
            if not plain:
                for index in self.by_import.get(name, ()):
                    scores[index] += 0.5
                if len(identifier) >= 5:
                    needle = identifier.lower()
                    for index, chunk in enumerate(self.chunks):
                        if chunk.symbol and needle in chunk.symbol.lower() and index not in scores:
                            scores[index] += 1.0
        ranked = sorted(
            scores.items(),
            key=lambda pair: (
                -pair[1],
                self.chunks[pair[0]].is_test,
                self.chunks[pair[0]].lines,
                pair[0],
            ),
        )
        return [(index, score) for index, score in ranked[:n] if score > 0]
