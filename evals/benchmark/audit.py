"""Report audit, site audit and redaction for Bench v1 (docs/bench-v1-design.md §6).

Everything here is mechanical and runs against the tree the agent will see
(the buggy tree of a mutation task, the base tree of a real one), using the
same symbol table the agent's ``search_symbol`` tool uses.  Three jobs:

* ``audit_report`` -- refuse a bug report that names what the tier forbids
  (the changed symbol, the changed file, a traceback frame inside the
  package, a private name, anything below the entry points of a
  ``symptom_only`` report), and derive what it *does* name: the surface
  symbols and files, and whether the fix is cross-module.
* ``site_notes`` -- point the author at comments, docstrings, changelog lines
  and textbook shapes that would give a mutation site away (§4.1); printed,
  never enforced, because the judgement is the author's.
* ``redact`` -- the report with every repository symbol replaced by a neutral
  placeholder, for the leak ablation's arm B′ (§6.4 / §9.2).

Only whole identifiers count.  The name in the *source* is what the agent's
tools find, so a public alias of a changed symbol (the ``truncate`` filter for
``do_truncate``) is allowed and a sub-token overlap is a warning, not a
violation.
"""

from __future__ import annotations

import ast
import io
import keyword
import re
import tokenize
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from evals.benchmark.schema import ReportLevel, touched_files
from repopilot.tools.code import Symbol
from repopilot.tools.paths import is_test_path

# -- identifiers in prose -------------------------------------------------------

_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
# The forms a report uses to name code: dotted names, snake_case (an inner
# underscore), private names, CamelCase (an inner capital) and calls.
_IDENT_RE = re.compile(
    r"(?<![\w.])("
    r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+"  # dotted
    r"|[A-Za-z_]*[a-z\d]_[a-z\d][\w]*"  # snake_case
    r"|_[A-Za-z][\w]*"  # a private name
    r"|[A-Z][A-Za-z\d]*[A-Z][A-Za-z\d]*"  # CamelCase: at least two capitals
    r"|[A-Za-z_][\w]*(?=\()"  # a call
    r")"
)
_BARE_RE = re.compile(r"(?<![\w.])([A-Za-z_][\w]*)(?![\w.])")
_PYTHON_FENCES = frozenset({"python", "py", "python3", "pycon"})
_TRACEBACK_RE = re.compile(r'File\s+"[^"\n]+\.py"|[\w./-]+\.py(?::\d+|,\s*line\s+\d+)')
_WORD_RE = re.compile(r"[A-Za-z][a-z\d]+|[A-Z]+(?![a-z])|\d+")
_STOP_TOKENS = frozenset(
    {
        "self",
        "return",
        "none",
        "true",
        "false",
        "the",
        "and",
        "not",
        "for",
        "with",
        "from",
        "import",
        "def",
        "class",
        "else",
        "elif",
        "this",
        "that",
        "when",
        "then",
        "value",
        "values",
        "item",
        "items",
        "key",
        "keys",
        "args",
        "kwargs",
        "cls",
        "type",
        "name",
        "data",
        "result",
        "should",
        "must",
        "only",
        "also",
        "into",
        "each",
        "same",
        "than",
    }
)


@dataclass(frozen=True)
class Mention:
    """An identifier-shaped span of the report."""

    text: str
    start: int
    end: int
    code: bool  # inside a back-ticked span

    @property
    def last(self) -> str:
        return self.text.rsplit(".", 1)[-1]


def mentions(report: str) -> list[Mention]:
    """Identifier-shaped spans of ``report``, in order, one per position.

    Inside back-ticks and Python fenced blocks every identifier counts; in
    prose and in other fenced blocks (a shell transcript, program output) only
    dotted names, snake_case, private and CamelCase words and calls do (a bare
    word like "format" in a sentence is English until it is quoted).
    """
    found: dict[int, Mention] = {}
    code_regions: list[tuple[int, int]] = []
    output_regions: list[tuple[int, int]] = []
    for fence in _FENCE_RE.finditer(report):
        full = fence.group(0)
        first_line = full[3:].split("\n", 1)[0].strip().lower()
        inner_start = full.find("\n") + 1 if "\n" in full else 3
        inner_end = max(inner_start, len(full) - 3)
        region = (fence.start() + inner_start, fence.start() + inner_end)
        # A Python block is code; a shell transcript or plain output block is
        # prose ("Error: Got unexpected extra argument" names no symbol).
        if first_line in _PYTHON_FENCES:
            code_regions.append(region)
        else:
            output_regions.append(region)
    fenced_out = _FENCE_RE.sub(lambda m: " " * len(m.group(0)), report)
    for span in _CODE_SPAN_RE.finditer(fenced_out):
        code_regions.append((span.start(1), span.end(1)))
    for start, end in code_regions:
        body = report[start:end]
        for m in _BARE_RE.finditer(body):
            _add(found, m.group(1), start + m.start(1), True)
        for m in _IDENT_RE.finditer(body):
            _add(found, m.group(1), start + m.start(1), True)
    for start, end in output_regions:
        body = report[start:end]
        for m in _IDENT_RE.finditer(body):
            _add(found, m.group(1), start + m.start(1), True)
    prose = _CODE_SPAN_RE.sub(lambda m: " " * len(m.group(0)), fenced_out)
    for m in _IDENT_RE.finditer(prose):
        _add(found, m.group(1), m.start(1), False)
    return [found[k] for k in sorted(found)]


def _add(found: dict[int, Mention], text: str, start: int, code: bool) -> None:
    if keyword.iskeyword(text) or text in ("None", "True", "False"):
        return
    if len(text) < 2 or text.isdigit():
        return
    existing = found.get(start)
    if existing is None or len(text) > len(existing.text):
        found[start] = Mention(text, start, start + len(text), code)


# -- the symbol table ---------------------------------------------------------------


@dataclass
class SymbolTable:
    """The definitions of a tree, with the lookups the audit needs."""

    symbols: tuple[Symbol, ...]

    def __post_init__(self) -> None:
        self._source = [
            s for s in self.symbols if not is_test_path(s.path) and not is_ancillary_path(s.path)
        ]
        self._by_name: dict[str, list[Symbol]] = {}
        self._by_qual: dict[str, list[Symbol]] = {}
        for s in self._source:
            self._by_name.setdefault(s.name, []).append(s)
            self._by_qual.setdefault(s.qualname, []).append(s)

    @classmethod
    def from_workspace(cls, workspace: object) -> SymbolTable:
        from repopilot.tools.code import SymbolIndex

        return cls(tuple(SymbolIndex(workspace).symbols))  # type: ignore[arg-type]

    @classmethod
    def from_files(cls, files: Mapping[str, str]) -> SymbolTable:
        """A table over ``{path: source}``; unparseable files are skipped."""
        from repopilot.tools.code import _symbols_in

        symbols: list[Symbol] = []
        for path, text in files.items():
            if not path.endswith(".py"):
                continue
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            symbols.extend(_symbols_in(path, tree))
        return cls(tuple(symbols))

    def resolve(self, text: str) -> list[Symbol]:
        """Definitions ``text`` names: an exact name, a qualified name, or a qualified
        suffix.  A dotted name whose first part is a lower-case instance
        (``table.add_row``) is also tried as its class (``Table.add_row``); a
        leading package path (``sqlparse.format``) is stripped."""
        candidates = [text]
        parts = text.split(".")
        if len(parts) > 1:
            if parts[0][:1].islower():
                candidates.append(".".join([parts[0][:1].upper() + parts[0][1:], *parts[1:]]))
            for i in range(1, len(parts)):
                candidates.append(".".join(parts[i:]))
        seen: set[tuple[str, str]] = set()
        out: list[Symbol] = []
        for cand in candidates:
            hits = list(self._by_qual.get(cand, ()))
            if not hits and "." not in cand:
                hits = list(self._by_name.get(cand, ()))
            if not hits and "." in cand:
                hits = [s for s in self._source if s.qualname.endswith("." + cand)]
            for s in hits:
                key = (s.path, s.qualname)
                if key not in seen:
                    seen.add(key)
                    out.append(s)
            if out:
                break  # the most specific candidate that resolves wins
        return out


_ANCILLARY_DIRS = frozenset({"examples", "example", "docs", "doc", "benchmarks", "scripts"})


def is_ancillary_path(path: str) -> bool:
    """Examples, docs and scripts define symbols too, but they are not the library:
    a report's ``cli`` should not resolve to ``examples/naval/naval.py``."""
    return bool(set(path.split("/")[:-1]) & _ANCILLARY_DIRS)


_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)", re.MULTILINE)
_CLASS_RE = re.compile(r"^\s*class\s+(\w+)", re.MULTILINE)
_ASSIGN_RE = re.compile(r"^\s*(\w+)\s*(?::[^=\n]+)?=(?!=)", re.MULTILINE)


def user_defined_names(report: str) -> set[str]:
    """Names the report's own code defines -- its ``def cli(greeting, name)``, its
    classes, its assignments -- which are the user's, not the repository's."""
    names: set[str] = set()
    for m in _DEF_RE.finditer(report):
        names.add(m.group(1))
        for param in m.group(2).split(","):
            param = param.strip().lstrip("*").split(":")[0].split("=")[0].strip()
            if param:
                names.add(param)
    names.update(m.group(1) for m in _CLASS_RE.finditer(report))
    names.update(m.group(1) for m in _ASSIGN_RE.finditer(report))
    names.discard("self")
    return names


def is_private(symbol: Symbol) -> bool:
    """A leading underscore anywhere in the qualified name or the module path."""
    if any(part.startswith("_") for part in symbol.qualname.split(".")):
        return True
    return any(part.startswith("_") and part != "__init__.py" for part in symbol.path.split("/"))


# -- the report audit ---------------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    mention: Mention
    symbols: tuple[Symbol, ...]


@dataclass(frozen=True)
class AuditResult:
    violations: tuple[str, ...]
    warnings: tuple[str, ...]
    surface_symbols: tuple[str, ...]
    surface_files: tuple[str, ...]
    cross_module: bool
    resolved: tuple[Resolved, ...] = field(default=(), repr=False)

    @property
    def ok(self) -> bool:
        return not self.violations

    def format(self) -> str:
        lines = [f"report audit: {'ok' if self.ok else 'FAILED'}"]
        lines += [f"  violation: {v}" for v in self.violations]
        lines += [f"  warning: {w}" for w in self.warnings]
        lines.append(f"  surface symbols: {', '.join(self.surface_symbols) or '-'}")
        lines.append(f"  surface files: {', '.join(self.surface_files) or '-'}")
        lines.append(f"  cross_module: {self.cross_module}")
        return "\n".join(lines)


def _gold_names(gold_symbols: Iterable[str]) -> set[str]:
    names: set[str] = set()
    for symbol in gold_symbols:
        names.add(symbol.lower())
        names.add(symbol.rsplit(".", 1)[-1].lower())
    return names


def _gold_file_forms(gold_files: Iterable[str]) -> list[str]:
    forms: list[str] = []
    for path in gold_files:
        forms.append(path)
        if path.startswith("src/"):
            forms.append(path[4:])
        name = path.rsplit("/", 1)[-1]
        if name != "__init__.py":
            forms.append(name)
    return forms


def _sub_tokens(text: str) -> set[str]:
    return {
        t.lower() for t in _WORD_RE.findall(text) if len(t) >= 4 and t.lower() not in _STOP_TOKENS
    }


def _allowed_entry_point(
    mention: Mention, symbols: Sequence[Symbol], entry_points: Sequence[str]
) -> bool:
    text = mention.text
    for ep in entry_points:
        if text == ep or ep.endswith("." + text) or text.endswith("." + ep):
            return True
        head = ep.split(".")[0]
        for s in symbols:
            if s.qualname == ep or ep.endswith("." + s.qualname):
                return True
            if s.kind == "class" and s.qualname == head and head != ep:
                return True
    return False


def audit_report(
    report: str,
    table: SymbolTable,
    *,
    gold_files: Sequence[str],
    gold_symbols: Sequence[str],
    report_level: ReportLevel | str = ReportLevel.INTERNAL,
    entry_points: Sequence[str] = (),
) -> AuditResult:
    """Check ``report`` against its tier and derive the surface it offers."""
    level = ReportLevel(report_level)
    violations: list[str] = []
    warnings: list[str] = []
    gold_names = _gold_names(gold_symbols)
    own = user_defined_names(report)
    resolved: list[Resolved] = []
    for mention in mentions(report):
        # A bare name the report's own code defines is the user's; a dotted or
        # qualified use still resolves (``click.echo`` is the library's even if
        # the user also has a variable called ``echo``).
        if "." not in mention.text and mention.text in own:
            symbols: tuple[Symbol, ...] = ()
        else:
            symbols = tuple(table.resolve(mention.text))
        resolved.append(Resolved(mention, symbols))

    if level is not ReportLevel.INTERNAL:
        for path in _gold_file_forms(gold_files):
            if path in report:
                violations.append(f"names the changed file: {path!r}")
                break
        for m in _TRACEBACK_RE.finditer(report):
            violations.append(f"traceback frame or file:line reference: {m.group(0)!r}")
            break
        seen_names: set[str] = set()
        for r in resolved:
            text = r.mention.text
            names = {text.lower(), r.mention.last.lower()}
            if names & gold_names and text not in seen_names:
                seen_names.add(text)
                violations.append(f"names a changed symbol: {text!r}")
                continue
            if not r.symbols:
                continue
            if level is ReportLevel.SYMPTOM_ONLY:
                allowed = _allowed_entry_point(r.mention, r.symbols, entry_points)
                if not allowed and text not in seen_names:
                    seen_names.add(text)
                    where = ", ".join(f"{s.qualname} ({s.path})" for s in r.symbols[:3])
                    violations.append(
                        f"symptom_only report names {text!r}, which is not an entry point ({where})"
                    )
            elif all(is_private(s) for s in r.symbols) and text not in seen_names:
                seen_names.add(text)
                where = ", ".join(f"{s.qualname} ({s.path})" for s in r.symbols[:3])
                violations.append(f"names a private symbol: {text!r} -> {where}")

    # Warnings at every tier: sub-token overlap with a changed symbol, ambiguity.
    gold_tokens = {t for g in gold_symbols for t in _sub_tokens(g.rsplit(".", 1)[-1])}
    warned: set[str] = set()
    for r in resolved:
        text = r.mention.text
        if text in warned:
            continue
        if text.lower() not in gold_names and _sub_tokens(r.mention.last) & gold_tokens:
            warned.add(text)
            shared = ", ".join(sorted(_sub_tokens(r.mention.last) & gold_tokens))
            warnings.append(f"{text!r} shares a sub-token with a changed symbol ({shared})")
        files = {s.path for s in r.symbols}
        if len(files) > 1:
            warned.add(text)
            warnings.append(
                f"{text!r} resolves to definitions in {len(files)} files "
                f"({', '.join(sorted(files)[:4])}); qualify it to make the surface exact"
            )

    surface_symbols = sorted({s.qualname for r in resolved for s in r.symbols})
    surface_files = sorted({s.path for r in resolved for s in r.symbols})
    if not surface_files and entry_points:
        for ep in entry_points:
            for s in table.resolve(ep):
                surface_symbols.append(s.qualname)
                surface_files.append(s.path)
        surface_symbols = sorted(set(surface_symbols))
        surface_files = sorted(set(surface_files))
        if surface_files:
            warnings.append("the report names no repository symbol; surface = entry points")
    if not surface_files:
        warnings.append(
            "the report names no repository symbol and no entry point resolves; "
            "cross_module cannot be derived (recorded as False)"
        )
    cross_module = bool(surface_files) and not (set(surface_files) & set(gold_files))
    if surface_files and not cross_module:
        warnings.append(
            "same-module: the report names something defined in a gold file "
            f"({', '.join(sorted(set(surface_files) & set(gold_files)))})"
        )
    return AuditResult(
        violations=tuple(violations),
        warnings=tuple(warnings),
        surface_symbols=tuple(surface_symbols),
        surface_files=tuple(surface_files),
        cross_module=cross_module,
        resolved=tuple(resolved),
    )


# -- redaction ----------------------------------------------------------------------

_PLACEHOLDER = {
    "class": "a class",
    "function": "a function",
    "method": "a method",
    "variable": "a value",
}


def redact(
    report: str,
    table: SymbolTable,
    *,
    gold_files: Sequence[str] = (),
) -> str:
    """``report`` with every repository symbol replaced by a placeholder.

    A back-ticked span or fenced block that contains one becomes ``(code)``;
    in prose the identifier becomes "a function" / "a class" / "a method".
    Entry points are redacted too: arm B′ measures what the identifiers were
    worth, all of them.
    """
    own = user_defined_names(report)
    resolved = [
        r
        for r in (
            Resolved(m, () if "." not in m.text and m.text in own else tuple(table.resolve(m.text)))
            for m in mentions(report)
        )
        if r.symbols
    ]
    if not resolved and not gold_files:
        return report
    spans: list[tuple[int, int, str]] = []
    covered: list[tuple[int, int]] = []
    for region in (*_FENCE_RE.finditer(report), *_CODE_SPAN_RE.finditer(report)):
        start, end = region.span()
        if any(start >= s and end <= e for s, e in covered):
            continue
        if any(start <= r.mention.start < end for r in resolved):
            spans.append((start, end, "(code)"))
            covered.append((start, end))
    for r in resolved:
        m = r.mention
        if any(s <= m.start < e for s, e in covered):
            continue
        end = _call_end(report, m.end)
        spans.append((m.start, end, _PLACEHOLDER.get(r.symbols[0].kind, "a symbol")))
    text = report
    for path in _gold_file_forms(gold_files):
        for m in list(re.finditer(re.escape(path), text)):
            if not any(s <= m.start() < e for s, e, _ in spans):
                spans.append((m.start(), m.end(), "a file"))
    for start, end, replacement in sorted(spans, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _call_end(text: str, end: int, limit: int = 60) -> int:
    """Extend a mention over its argument list when it is written as a call."""
    if end >= len(text) or text[end] != "(":
        return end
    depth = 0
    for i in range(end, min(len(text), end + limit)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        elif text[i] == "\n":
            break
    return end


GENERIC_REPORT = "There is one injected bug in this repository; find and fix it."


# -- the site audit -----------------------------------------------------------------

_BANNED_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mutable default argument", re.compile(r"def\s+\w+\(.*=\s*(\{\}|\[\]|set\(\))")),
    (
        "`if not x` / `if x` -- a None check written as an emptiness check?",
        re.compile(r"^\s*if\s+(not\s+)?\w+\s*:\s*$"),
    ),
    (
        "`type(x) ==` / `type(x) in` instead of isinstance",
        re.compile(r"type\(\w+\)\s*(==|!=|in\b)"),
    ),
    ("unguarded `del d[key]`", re.compile(r"^\s*del\s+\w+\[")),
    ("bare `except:`", re.compile(r"^\s*except\s*:")),
    ("`== None` / `!= None`", re.compile(r"(==|!=)\s*None\b")),
)
_CHANGELOG_NAMES = ("CHANGES", "CHANGELOG", "HISTORY", "NEWS")


def _hunk_lines(patch: str) -> dict[str, tuple[set[int], set[int]]]:
    """Old-side line numbers per file: (removed or modified, insertion points)."""
    from evals.benchmark.authoring import changed_lines

    changed = changed_lines(patch)
    out: dict[str, tuple[set[int], set[int]]] = {}
    for path in touched_files(patch):
        out[path] = (changed.modified.get(path, set()), changed.inserted_before.get(path, set()))
    return out


def _comment_and_docstring_lines(text: str) -> dict[int, str]:
    """Line number -> text for every comment line and docstring line of a module."""
    prose: dict[int, str] = {}
    lines = text.splitlines()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                prose[tok.start[0]] = tok.string
    except (tokenize.TokenError, SyntaxError):
        pass
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return prose
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                doc = body[0]
                for line in range(doc.lineno, (doc.end_lineno or doc.lineno) + 1):
                    if 1 <= line <= len(lines):
                        prose[line] = lines[line - 1]
    return prose


def site_notes(
    gold_patch: str,
    files: Mapping[str, str],
    *,
    gold_symbols: Sequence[str] = (),
    changelog: str | None = None,
    window: int = 8,
) -> list[str]:
    """What could give the site away (docs/bench-v1-design.md §4.1), for the author.

    ``files`` maps each gold file to its text in the tree the agent sees (the
    old side of ``gold_patch``).
    """
    notes: list[str] = []
    removed_by_file: dict[str, list[str]] = {}
    current: str | None = None
    for line in gold_patch.splitlines():
        if line.startswith("diff --git "):
            current = line.split(" b/", 1)[-1] if " b/" in line else None
            continue
        if current and line.startswith("-") and not line.startswith("---"):
            removed_by_file.setdefault(current, []).append(line[1:])
    for path, (modified, inserted) in _hunk_lines(gold_patch).items():
        text = files.get(path)
        if text is None:
            continue
        lines = text.splitlines()
        anchors = sorted(modified | inserted)
        changed_text = " ".join(lines[i - 1] for i in modified if 1 <= i <= len(lines))
        changed_text += " " + " ".join(removed_by_file.get(path, []))
        tokens = _sub_tokens(changed_text)
        prose = _comment_and_docstring_lines(text)
        seen: set[int] = set()
        for anchor in anchors:
            for line_no in range(max(1, anchor - window), min(len(lines), anchor + window) + 1):
                if line_no in seen or line_no in modified:
                    continue
                prose_line = prose.get(line_no)
                if prose_line is None:
                    continue
                shared = tokens & _sub_tokens(prose_line)
                if len(shared) >= 2:
                    seen.add(line_no)
                    notes.append(
                        f"{path}:{line_no} prose near the site shares "
                        f"{', '.join(sorted(shared))}: {prose_line.strip()[:100]!r}"
                    )
        for removed in removed_by_file.get(path, []):
            for label, pattern in _BANNED_SHAPES:
                if pattern.search(removed):
                    notes.append(
                        f"{path}: buggy line matches a banned shape ({label}): "
                        f"{removed.strip()[:80]!r}"
                    )
    if changelog:
        names = {g.rsplit(".", 1)[-1] for g in gold_symbols if len(g.rsplit(".", 1)[-1]) >= 4}
        for line_no, line in enumerate(changelog.splitlines(), 1):
            for name in sorted(names):
                if name in line:
                    notes.append(
                        f"changelog line {line_no} mentions {name!r}: {line.strip()[:100]!r}"
                    )
                    break
            if len(notes) > 60:
                break
    return notes


def find_changelog(files: Iterable[str]) -> str | None:
    """The repository's changelog path at the root, if any."""
    for path in sorted(files):
        if "/" in path:
            continue
        stem = path.split(".")[0].upper()
        if stem in _CHANGELOG_NAMES:
            return path
    return None
