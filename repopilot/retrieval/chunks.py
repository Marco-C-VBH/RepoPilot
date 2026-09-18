"""Semantic code chunks (spec §7.1), from Python's ``ast``.

One chunk per function, method, class and module, non-overlapping:

* function / method -- the ``def`` with its decorators and body (nested
  functions stay inside their parent);
* class -- the header, docstring and class-level statements, plus one line per
  method signature (the bodies are chunks of their own);
* module -- docstring, imports and the top-level statements outside any
  ``def`` / ``class``.

Functions longer than ``MAX_CHUNK_LINES`` are split into windows that keep the
symbol's metadata (``part`` 1..n).  A file ``ast`` cannot parse is chunked as
raw windows of text, so nothing in the repository is invisible to retrieval.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field

from repopilot.tools.paths import PathError, is_test_path
from repopilot.tools.workspace import Workspace

MAX_CHUNK_LINES = 120  # longer function bodies are split into windows of this size
MODULE_CHUNK_LINES = 200  # a module skeleton is clipped here


@dataclass(frozen=True)
class Chunk:
    path: str
    symbol: str  # qualified name; "" for the module chunk
    symbol_type: str  # module | class | function | method
    start_line: int
    end_line: int
    text: str
    imports: tuple[str, ...] = ()
    is_test: bool = False
    part: int = 0  # > 0 when split from a longer symbol
    defines: tuple[str, ...] = field(default=())  # names this chunk defines
    numbers: tuple[int, ...] = ()  # line number per text line when not contiguous

    @property
    def id(self) -> str:
        suffix = f"#{self.part}" if self.part else ""
        return f"{self.path}:{self.start_line}-{self.end_line}{suffix}"

    @property
    def title(self) -> str:
        name = self.symbol or "(module)"
        return f"{self.path} {self.symbol_type} {name}"

    @property
    def name(self) -> str:
        """The unqualified name (``method`` of ``Class.method``)."""
        return self.symbol.rsplit(".", 1)[-1] if self.symbol else ""

    @property
    def lines(self) -> int:
        return self.end_line - self.start_line + 1

    def numbered(self) -> list[tuple[int, str]]:
        """``(line number, text)`` per line of ``text``; skeletons carry their own numbers."""
        lines = self.text.splitlines()
        if self.numbers:
            return list(zip(self.numbers, lines, strict=False))
        return [(self.start_line + i, line) for i, line in enumerate(lines)]

    def text_hash(self) -> str:
        """Stable key for embedding caches: the same text embeds the same."""
        return hashlib.sha1((self.title + "\n" + self.text).encode("utf-8")).hexdigest()

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "path": self.path,
            "symbol": self.symbol,
            "symbol_type": self.symbol_type,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "lines": self.lines,
            "is_test": self.is_test,
        }


def chunk_workspace(workspace: Workspace) -> list[Chunk]:
    """Every Python file of the workspace, chunked; tests included (flagged)."""
    chunks: list[Chunk] = []
    for path in workspace.files():
        if not path.endswith(".py"):
            continue
        try:
            text = workspace.read_text(path)
        except PathError:
            continue
        chunks.extend(chunk_module(path, text))
    return chunks


def chunk_module(path: str, text: str) -> list[Chunk]:
    """Chunks for one file (see the module docstring)."""
    is_test = is_test_path(path)
    lines = text.splitlines()
    if not lines:
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _raw_windows(path, lines, is_test)

    imports = tuple(_imports(tree))
    chunks: list[Chunk] = []
    skeleton: list[tuple[int, str]] = []
    defined: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.append(node.name)
            chunks.extend(
                _function_chunks(path, lines, node, (node.name,), "function", imports, is_test)
            )
        elif isinstance(node, ast.ClassDef):
            defined.append(node.name)
            chunks.extend(_class_chunks(path, lines, node, (node.name,), imports, is_test))
        else:
            skeleton.extend(_source(lines, node))
            defined.extend(_assigned_names(node))
    skeleton = [(n, line) for n, line in skeleton if line.strip()]
    if len(skeleton) > MODULE_CHUNK_LINES:
        skeleton = skeleton[:MODULE_CHUNK_LINES] + [(skeleton[MODULE_CHUNK_LINES][0], "...")]
    if skeleton or not chunks:
        chunks.insert(
            0,
            Chunk(
                path,
                "",
                "module",
                1,
                len(lines),
                "\n".join(line for _, line in skeleton) or "(empty module)",
                imports,
                is_test,
                defines=tuple(defined),
                numbers=tuple(n for n, _ in skeleton),
            ),
        )
    return chunks


# ------------------------------------------------------------------------ builders


def _function_chunks(
    path: str,
    lines: list[str],
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    scope: tuple[str, ...],
    kind: str,
    imports: tuple[str, ...],
    is_test: bool,
) -> list[Chunk]:
    start = _first_line(node)
    end = node.end_lineno or node.lineno
    symbol = ".".join(scope)
    body = lines[start - 1 : end]
    if len(body) <= MAX_CHUNK_LINES:
        return [Chunk(path, symbol, kind, start, end, "\n".join(body), imports, is_test)]
    chunks = []
    for part, offset in enumerate(range(0, len(body), MAX_CHUNK_LINES), 1):
        window = body[offset : offset + MAX_CHUNK_LINES]
        chunks.append(
            Chunk(
                path,
                symbol,
                kind,
                start + offset,
                start + offset + len(window) - 1,
                "\n".join(window),
                imports,
                is_test,
                part=part,
            )
        )
    return chunks


def _class_chunks(
    path: str,
    lines: list[str],
    node: ast.ClassDef,
    scope: tuple[str, ...],
    imports: tuple[str, ...],
    is_test: bool,
) -> list[Chunk]:
    start = _first_line(node)
    end = node.end_lineno or node.lineno
    symbol = ".".join(scope)
    header_end = node.body[0].lineno - 1 if node.body else end
    skeleton = [(n, lines[n - 1]) for n in range(start, header_end + 1)]
    chunks: list[Chunk] = []
    defined: list[str] = []
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.append(child.name)
            skeleton.append((child.lineno, lines[child.lineno - 1].rstrip() + " ..."))
            chunks.extend(
                _function_chunks(
                    path, lines, child, (*scope, child.name), "method", imports, is_test
                )
            )
        elif isinstance(child, ast.ClassDef):
            defined.append(child.name)
            skeleton.append((child.lineno, lines[child.lineno - 1].rstrip() + " ..."))
            chunks.extend(_class_chunks(path, lines, child, (*scope, child.name), imports, is_test))
        else:
            skeleton.extend(_source(lines, child))
            defined.extend(_assigned_names(child))
    chunks.insert(
        0,
        Chunk(
            path,
            symbol,
            "class",
            start,
            end,
            "\n".join(line for _, line in skeleton),
            imports,
            is_test,
            defines=tuple(defined),
            numbers=tuple(n for n, _ in skeleton),
        ),
    )
    return chunks


def _raw_windows(path: str, lines: list[str], is_test: bool) -> list[Chunk]:
    chunks = []
    for part, offset in enumerate(range(0, len(lines), MAX_CHUNK_LINES), 1):
        window = lines[offset : offset + MAX_CHUNK_LINES]
        chunks.append(
            Chunk(
                path,
                "",
                "module",
                offset + 1,
                offset + len(window),
                "\n".join(window),
                (),
                is_test,
                part=part if len(lines) > MAX_CHUNK_LINES else 0,
            )
        )
    return chunks


# -------------------------------------------------------------------------- helpers


def _first_line(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    decorators = [d.lineno for d in node.decorator_list]
    return min([node.lineno, *decorators])


def _source(lines: list[str], node: ast.AST) -> list[tuple[int, str]]:
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", start) or start
    return [(n, lines[n - 1]) for n in range(start, end + 1)]


def _imports(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.append(alias.asname or alias.name)
    seen: dict[str, None] = {}
    for name in names:
        seen.setdefault(name, None)
    return list(seen)


def _assigned_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []
