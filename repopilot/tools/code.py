"""Read, search, locate and edit code in a Workspace.

These are the Phase 1 baseline implementations of the spec's tool surface (§6):
plain lexical search and an ``ast``-based symbol table.  They are deliberately
simple -- the retrieval work in Phase 3 (BM25, embeddings, symbol graph) will
replace ``search_code`` behind the same interface, so the baseline numbers and
the retrieval numbers stay comparable.

Every function takes the workspace and plain arguments and returns a
``ToolResult``; anything the model got wrong becomes an error result with a
hint, not an exception.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from repopilot.tools.paths import PathError, is_test_path, is_text_path
from repopilot.tools.results import ToolResult
from repopilot.tools.workspace import Workspace

MAX_READ_LINES = 250
MAX_FILE_BYTES = 1_000_000  # larger files are skipped by search
SYMBOL_KINDS = ("function", "method", "class", "variable")


# -------------------------------------------------------------------------- read_file


def read_file(
    workspace: Workspace, path: str, start: int = 1, end: int | None = None
) -> ToolResult:
    """A numbered, bounded slice of a file (at most ``MAX_READ_LINES`` lines)."""
    try:
        text = workspace.read_text(path)
    except PathError as exc:
        return ToolResult.error(str(exc))
    lines = text.splitlines()
    total = len(lines)
    if not isinstance(start, int) or start < 1:
        return ToolResult.error("start must be a positive line number")
    if end is not None and (not isinstance(end, int) or end < start):
        return ToolResult.error("end must be a line number >= start")
    if total == 0:
        return ToolResult(f"{path} is empty", meta={"path": path, "lines": 0})
    if start > total:
        return ToolResult.error(f"{path} has only {total} lines")
    last = min(end if end is not None else start + MAX_READ_LINES - 1, total)
    if last - start + 1 > MAX_READ_LINES:
        last = start + MAX_READ_LINES - 1
    header = f"{path} lines {start}-{last} of {total}"
    if last < total and (end is None or end > last):
        header += f" (call again with start={last + 1} for more)"
    body = "\n".join(f"{n:>5}| {lines[n - 1]}" for n in range(start, last + 1))
    return ToolResult(
        f"{header}\n{body}",
        meta={"path": path, "start": start, "end": last, "lines": total},
    )


# ------------------------------------------------------------------------ search_code


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    text: str


def _searchable_files(workspace: Workspace) -> list[str]:
    files = [p for p in workspace.files() if is_text_path(p)]
    # Source before tests: the bug is in the source, and tests only tell you where it shows.
    return sorted(files, key=lambda p: (is_test_path(p), p))


def _scan(workspace: Workspace, pattern: re.Pattern[str], limit: int) -> tuple[list[Hit], int]:
    hits: list[Hit] = []
    total = 0
    for path in _searchable_files(workspace):
        file = workspace.root / path
        try:
            if file.stat().st_size > MAX_FILE_BYTES:
                continue
            text = workspace.read_text(path)
        except (PathError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                total += 1
                if len(hits) < limit:
                    hits.append(Hit(path, number, line.strip()))
    return hits, total


def search_code(
    workspace: Workspace, query: str, top_k: int = 10, regex: bool = False
) -> ToolResult:
    """Lexical search: lines containing ``query`` (case-insensitive) or matching a regex."""
    if not isinstance(query, str) or not query.strip():
        return ToolResult.error("query must be a non-empty string")
    top_k = _clamp_int(top_k, 1, 50, default=10)
    try:
        pattern = re.compile(query if regex else re.escape(query.strip()), re.IGNORECASE)
    except re.error as exc:
        return ToolResult.error(f"invalid regex: {exc}")
    hits, total = _scan(workspace, pattern, top_k)
    if not hits:
        return ToolResult(
            f"no matches for {query!r}", meta={"query": query, "hits": 0, "files": []}
        )
    lines = [f"{h.path}:{h.line}: {_shorten(h.text, 160)}" for h in hits]
    if total > len(hits):
        lines.append(f"... {total - len(hits)} more matches; narrow the query or raise top_k")
    return ToolResult(
        "\n".join(lines),
        meta={
            "query": query,
            "hits": total,
            "shown": len(hits),
            "files": sorted({h.path for h in hits}),
        },
    )


# ---------------------------------------------------------------------- symbol index


@dataclass(frozen=True)
class Symbol:
    path: str
    name: str
    qualname: str
    kind: str
    line: int
    end_line: int


class SymbolIndex:
    """Definitions of functions, methods, classes and module-level variables, from ``ast``.

    Rebuilt lazily whenever the workspace version changes (i.e. after an edit).
    """

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self._version: int | None = None
        self._symbols: list[Symbol] = []
        self.unparseable: list[str] = []

    @property
    def symbols(self) -> list[Symbol]:
        if self._version != self._workspace.version:
            self._build()
        return self._symbols

    def _build(self) -> None:
        symbols: list[Symbol] = []
        unparseable: list[str] = []
        for path in self._workspace.files():
            if not path.endswith(".py"):
                continue
            try:
                tree = ast.parse(self._workspace.read_text(path))
            except (SyntaxError, ValueError, PathError):
                unparseable.append(path)
                continue
            symbols.extend(_symbols_in(path, tree))
        self._symbols = symbols
        self.unparseable = unparseable
        self._version = self._workspace.version

    def lookup(self, name: str, kind: str | None = None) -> tuple[list[Symbol], bool]:
        """Exact matches on name or qualified name; falls back to case-insensitive
        substring matches (second element False) when nothing matches exactly."""
        wanted = [s for s in self.symbols if kind in (None, "any") or s.kind == kind]
        exact = [
            s
            for s in wanted
            if s.name == name or s.qualname == name or s.qualname.endswith("." + name)
        ]
        if exact:
            return exact, True
        needle = name.lower()
        return [s for s in wanted if needle in s.qualname.lower()], False


def _symbols_in(path: str, tree: ast.AST) -> list[Symbol]:
    found: list[Symbol] = []

    def visit(node: ast.AST, scope: tuple[str, ...], in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = ".".join((*scope, child.name))
                kind = "method" if in_class else "function"
                found.append(
                    Symbol(
                        path, child.name, qual, kind, child.lineno, child.end_lineno or child.lineno
                    )
                )
                visit(child, (*scope, child.name), False)
            elif isinstance(child, ast.ClassDef):
                qual = ".".join((*scope, child.name))
                found.append(
                    Symbol(
                        path,
                        child.name,
                        qual,
                        "class",
                        child.lineno,
                        child.end_lineno or child.lineno,
                    )
                )
                visit(child, (*scope, child.name), True)
            elif not scope and isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        found.append(
                            Symbol(
                                path,
                                target.id,
                                target.id,
                                "variable",
                                child.lineno,
                                child.end_lineno or child.lineno,
                            )
                        )
            elif not scope and isinstance(
                child, (ast.If, ast.Try, ast.ExceptHandler, ast.With, ast.For, ast.While)
            ):
                visit(child, scope, in_class)  # definitions guarded at module level

    visit(tree, (), False)
    return found


def search_symbol(
    workspace: Workspace, index: SymbolIndex, name: str, kind: str | None = None
) -> ToolResult:
    """Where a function, method, class or module-level variable is defined."""
    if not isinstance(name, str) or not name.strip():
        return ToolResult.error("name must be a non-empty string")
    name = name.strip()
    if kind is not None and kind not in (*SYMBOL_KINDS, "any"):
        return ToolResult.error(f"kind must be one of {', '.join(SYMBOL_KINDS)} or any")
    matches, exact = index.lookup(name, kind)
    if not matches:
        return ToolResult(
            f"no definition of {name!r} found (try search_code for string matches)",
            meta={"name": name, "hits": 0},
        )
    matches = sorted(matches, key=lambda s: (is_test_path(s.path), s.path, s.line))[:20]
    rows = [f"{s.path}:{s.line}-{s.end_line}  {s.kind}  {s.qualname}" for s in matches]
    prefix = "" if exact else f"no exact definition of {name!r}; similar names:\n"
    return ToolResult(
        prefix + "\n".join(rows),
        meta={"name": name, "hits": len(matches), "exact": exact},
    )


# -------------------------------------------------------------------- find_references


def find_references(workspace: Workspace, symbol: str, top_k: int = 20) -> ToolResult:
    """Lines that mention ``symbol`` as a whole word (calls, imports, attribute access)."""
    if not isinstance(symbol, str) or not symbol.strip():
        return ToolResult.error("symbol must be a non-empty string")
    top_k = _clamp_int(top_k, 1, 100, default=20)
    word = symbol.strip().rsplit(".", 1)[-1]  # Class.method -> method
    pattern = re.compile(rf"(?<![\w.]){re.escape(symbol.strip())}\b|\b{re.escape(word)}\b")
    hits, total = _scan(workspace, pattern, top_k)
    if not hits:
        return ToolResult(f"no references to {symbol!r}", meta={"symbol": symbol, "hits": 0})
    lines = [f"{h.path}:{h.line}: {_shorten(h.text, 160)}" for h in hits]
    if total > len(hits):
        lines.append(f"... {total - len(hits)} more")
    return ToolResult(
        "\n".join(lines),
        meta={"symbol": symbol, "hits": total, "files": sorted({h.path for h in hits})},
    )


# -------------------------------------------------------------------------- edit_file


def edit_file(workspace: Workspace, path: str, old_string: str, new_string: str) -> ToolResult:
    """Replace one exact occurrence of ``old_string`` in ``path`` with ``new_string``.

    Tests are read-only: the benchmark judges a fix with its own tests, so
    editing the suite can only hide the bug, never fix it.
    """
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return ToolResult.error("old_string and new_string must be strings")
    if not old_string:
        return ToolResult.error("old_string must not be empty (this tool edits existing text)")
    if old_string == new_string:
        return ToolResult.error("old_string and new_string are identical; nothing to change")
    try:
        workspace.resolve(path)
    except PathError as exc:
        return ToolResult.error(str(exc))
    if is_test_path(path):
        return ToolResult.error(
            f"{path} is a test file; tests are read-only in this task -- fix the source instead"
        )
    try:
        text = workspace.read_text(path)
    except PathError as exc:
        return ToolResult.error(str(exc))
    count = text.count(old_string)
    if count == 0:
        return ToolResult.error(
            f"old_string was not found in {path}; read the file and copy the exact text "
            "(indentation and whitespace included)"
        )
    if count > 1:
        return ToolResult.error(
            f"old_string occurs {count} times in {path}; include more surrounding lines so "
            "it matches exactly once"
        )
    offset = text.index(old_string)
    line = text.count("\n", 0, offset) + 1
    updated = text[:offset] + new_string + text[offset + len(old_string) :]
    workspace.write_text(path, updated)

    new_lines = updated.splitlines()
    first = max(line - 2, 1)
    last = min(line + new_string.count("\n") + 2, len(new_lines))
    snippet = "\n".join(f"{n:>5}| {new_lines[n - 1]}" for n in range(first, last + 1))
    return ToolResult(
        f"edited {path} at line {line}; the region now reads:\n{snippet}",
        meta={
            "path": path,
            "line": line,
            "removed_lines": old_string.count("\n") + 1,
            "added_lines": new_string.count("\n") + 1,
        },
    )


# ----------------------------------------------------------------------------- helpers


def _clamp_int(value: object, low: int, high: int, *, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(low, min(value, high))


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."
