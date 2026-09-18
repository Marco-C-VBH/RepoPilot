"""Code-aware tokenisation shared by the lexical channel and the hashing embedder.

An identifier contributes itself and its parts: ``getSizeOf`` -> ``getsizeof``,
``get``, ``size``, ``of``; ``test_missing_getsizeof`` -> the whole and
``test``, ``missing``, ``getsizeof``.  Everything is lower-cased; Python
keywords and one-character tokens are dropped.  No stemming: in code, ``expire``
and ``expired`` are different names.
"""

from __future__ import annotations

import keyword
import re

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_STOP = frozenset(keyword.kwlist) | {"self", "cls", "none", "true", "false"}


def split_identifier(word: str) -> list[str]:
    """``snake_case`` and ``CamelCase`` parts of an identifier, in order."""
    parts: list[str] = []
    for piece in word.split("_"):
        if not piece:
            continue
        parts.extend(_CAMEL.findall(piece) or [piece])
    return parts


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for match in _TOKEN.finditer(text):
        word = match.group(0)
        low = word.lower()
        if low in _STOP or len(low) < 2:
            continue
        out.append(low)
        parts = split_identifier(word)
        if len(parts) > 1:
            out.extend(p.lower() for p in parts if len(p) > 1 and p.lower() not in _STOP)
    return out
