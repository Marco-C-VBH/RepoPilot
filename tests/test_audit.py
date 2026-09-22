"""The report audit, the site audit and redaction (evals/benchmark/audit.py) on a
synthetic package -- no git, no Docker."""

from __future__ import annotations

import pytest

from evals.benchmark.audit import (
    GENERIC_REPORT,
    SymbolTable,
    audit_report,
    find_changelog,
    is_private,
    mentions,
    redact,
    site_notes,
)

# A pipeline-shaped package: a public entry point in __init__ that delegates to a
# private helper module, a class with public and private methods, a test file.
FILES = {
    "sqlp/__init__.py": (
        "from ._impl import helper\n\n\n"
        "def format(sql, reindent=False):\n"
        '    """Format SQL."""\n'
        "    return helper(sql, reindent)\n\n\n"
        "class Console:\n"
        "    def print(self, renderable):\n"
        "        return renderable\n"
    ),
    "sqlp/_impl.py": (
        "def helper(sql, reindent):\n"
        "    # reindent: the depth grows by one per nested statement\n"
        "    depth = 1 if reindent else 0\n"
        "    return sql\n\n\n"
        "class TokenList:\n"
        "    def token_next(self, index):\n"
        "        return index\n\n"
        "    def token_next_by(self, index):\n"
        "        return index\n"
    ),
    "sqlp/table.py": (
        "class Table:\n"
        '    """A table."""\n\n'
        "    def add_row(self, *cells):\n"
        "        self._rows.append(cells)\n\n"
        "    def _calculate_column_widths(self, width):\n"
        "        return [width // 2, width - width // 2]\n"
    ),
    "tests/test_format.py": "def test_format():\n    pass\n",
}
GOLD_FILES = ["sqlp/_impl.py"]
GOLD_SYMBOLS = ["helper"]


@pytest.fixture(scope="module")
def table() -> SymbolTable:
    return SymbolTable.from_files(FILES)


# -- mentions -----------------------------------------------------------------------


def test_mentions_find_code_spans_dotted_snake_and_camel_case() -> None:
    report = (
        "Calling `sqlp.format(sql, reindent=True)` on a `SELECT` indents twice; "
        "TokenList.token_next is unrelated, add_row() too, and TTLCache expires. "
        "Plain words like format and table are English here.\n\n"
        "```python\nfrom sqlp import format\nformat('select 1')\n```\n"
    )
    found = {m.text: m.code for m in mentions(report)}
    assert found["sqlp.format"] is True
    assert found["SELECT"] is True
    assert found["TokenList.token_next"] is False
    assert found["add_row"] is False
    assert found["TTLCache"] is False
    assert found["sqlp"] is True and found["format"] is True  # from the fenced block
    assert "table" not in found and "words" not in found  # prose stays prose


def test_mentions_skip_keywords_numbers_and_single_letters() -> None:
    assert [m.text for m in mentions("`if x is None: return 1`")] == []
    assert [m.text for m in mentions("`if xs is None: return 10`")] == ["xs"]


# -- resolution ---------------------------------------------------------------------


def test_resolve_strips_packages_and_capitalizes_instances(table: SymbolTable) -> None:
    assert [s.qualname for s in table.resolve("sqlp.format")] == ["format"]
    assert [s.qualname for s in table.resolve("console.print")] == ["Console.print"]
    assert [s.qualname for s in table.resolve("Table.add_row")] == ["Table.add_row"]
    assert [s.qualname for s in table.resolve("token_next")] == ["TokenList.token_next"]
    assert table.resolve("test_format") == []  # test symbols are not surface
    assert table.resolve("nothing_here") == []


def test_is_private_looks_at_names_and_module_paths(table: SymbolTable) -> None:
    by_qual = {s.qualname: s for s in table.symbols}
    assert is_private(by_qual["Table._calculate_column_widths"])
    assert is_private(by_qual["TokenList.token_next"])  # lives in _impl.py
    assert not is_private(by_qual["Table.add_row"])
    assert not is_private(by_qual["format"])


# -- the report audit ---------------------------------------------------------------


def test_internal_reports_pass_and_record_the_surface(table: SymbolTable) -> None:
    result = audit_report(
        "`helper()` in `sqlp/_impl.py` ignores `reindent`.",
        table,
        gold_files=GOLD_FILES,
        gold_symbols=GOLD_SYMBOLS,
        report_level="internal",
    )
    assert result.ok
    assert result.surface_symbols == ("helper",)
    assert result.surface_files == ("sqlp/_impl.py",)
    assert result.cross_module is False


def test_public_api_report_refuses_changed_symbol_file_frame_and_private_names(
    table: SymbolTable,
) -> None:
    report = (
        "`sqlp.format(sql, reindent=True)` indents twice. `helper` seems involved; see "
        'sqlp/_impl.py and `TokenList.token_next`.\n  File "sqlp/_impl.py", line 3\n'
    )
    result = audit_report(
        report, table, gold_files=GOLD_FILES, gold_symbols=GOLD_SYMBOLS, report_level="public_api"
    )
    assert not result.ok
    joined = "\n".join(result.violations)
    assert "changed file: 'sqlp/_impl.py'" in joined
    assert "traceback frame" in joined
    assert "changed symbol: 'helper'" in joined
    assert "private symbol: 'TokenList.token_next'" in joined


def test_public_api_report_that_names_only_the_entry_point_is_cross_module(
    table: SymbolTable,
) -> None:
    result = audit_report(
        "`sqlp.format('select 1', reindent=True)` returns the statement indented twice.",
        table,
        gold_files=GOLD_FILES,
        gold_symbols=GOLD_SYMBOLS,
        report_level="public_api",
    )
    assert result.ok
    assert result.surface_files == ("sqlp/__init__.py",)
    assert result.cross_module is True


def test_public_alias_of_a_changed_symbol_is_allowed_but_warned(table: SymbolTable) -> None:
    # The changed symbol is do_truncate; the report names the filter `truncate`.
    files = {"j/filters.py": "def do_truncate(s, n):\n    return s[:n]\n"}
    result = audit_report(
        "`{{ text|truncate(5) }}` keeps six characters.",
        SymbolTable.from_files(files),
        gold_files=["j/filters.py"],
        gold_symbols=["do_truncate"],
        report_level="public_api",
    )
    assert result.ok
    assert any("sub-token" in w for w in result.warnings)


def test_symptom_only_report_may_name_only_entry_points(table: SymbolTable) -> None:
    ok = audit_report(
        "`console.print(table)` drops the last column; `sqlp.format` is fine.",
        table,
        gold_files=["sqlp/table.py"],
        gold_symbols=["Table._calculate_column_widths"],
        report_level="symptom_only",
        entry_points=["Console.print", "sqlp.format"],
    )
    assert ok.ok
    assert ok.cross_module is True and "Console.print" in ok.surface_symbols
    bad = audit_report(
        "`table.add_row('a')` then `console.print(table)` drops the last column.",
        table,
        gold_files=["sqlp/table.py"],
        gold_symbols=["Table._calculate_column_widths"],
        report_level="symptom_only",
        entry_points=["Console.print"],
    )
    assert not bad.ok
    assert any("not an entry point" in v and "table.add_row" in v for v in bad.violations)


def test_symptom_only_report_naming_nothing_gets_the_entry_points_as_surface(
    table: SymbolTable,
) -> None:
    result = audit_report(
        "Formatting `SELECT 1` with reindent on gives two levels of indentation.",
        table,
        gold_files=GOLD_FILES,
        gold_symbols=GOLD_SYMBOLS,
        report_level="symptom_only",
        entry_points=["sqlp.format"],
    )
    assert result.ok
    assert result.surface_files == ("sqlp/__init__.py",)
    assert result.cross_module is True
    assert any("surface = entry points" in w for w in result.warnings)


def test_ambiguous_names_are_warned_and_count_conservatively() -> None:
    files = {
        "p/a.py": "class A:\n    def get(self):\n        pass\n",
        "p/b.py": "class B:\n    def get(self):\n        pass\n",
    }
    result = audit_report(
        "`get()` returns None.",
        SymbolTable.from_files(files),
        gold_files=["p/b.py"],
        gold_symbols=["B.get"],
        report_level="internal",
    )
    assert any("resolves to definitions in 2 files" in w for w in result.warnings)
    assert result.cross_module is False  # p/b.py is among the surface files


# -- redaction ----------------------------------------------------------------------


def test_redact_replaces_symbols_and_code_spans(table: SymbolTable) -> None:
    report = (
        "Calling `sqlp.format(sql, reindent=True)` indents twice; Table.add_row(1, 2) "
        "and the Console class show it. `SELECT 1` is the input.\n"
        "```python\nformat('x')\n```\n"
    )
    redacted = redact(report, table, gold_files=GOLD_FILES)
    assert "sqlp.format" not in redacted and "add_row" not in redacted
    # A single-capital word in prose is English to the audit (back-tick identifiers).
    assert "Calling (code) indents twice; a method and the Console class show it." in redacted
    assert "`SELECT 1` is the input." in redacted  # no symbol inside: kept
    assert redacted.endswith("(code)\n")
    assert redact("`Console` misbehaves; TokenList.token_next too.", table) == (
        "(code) misbehaves; a method too."
    )


def test_redact_leaves_a_report_without_symbols_alone(table: SymbolTable) -> None:
    text = "Nothing named here, just a symptom."
    assert redact(text, table) == text
    assert GENERIC_REPORT.startswith("There is one injected bug")


# -- the site audit -----------------------------------------------------------------

GOLD_PATCH = """\
diff --git a/sqlp/_impl.py b/sqlp/_impl.py
--- a/sqlp/_impl.py
+++ b/sqlp/_impl.py
@@ -1,4 +1,4 @@
 def helper(sql, reindent):
     # reindent: the depth grows by one per nested statement
-    depth = 1 if reindent else 0
+    depth = 2 if reindent else 0
     return sql
"""


def test_site_notes_flag_prose_changelog_and_banned_shapes() -> None:
    notes = site_notes(
        GOLD_PATCH,
        {"sqlp/_impl.py": FILES["sqlp/_impl.py"]},
        gold_symbols=["helper"],
        changelog="1.2: helper now reindents nested statements\n1.1: nothing\n",
    )
    joined = "\n".join(notes)
    assert "prose near the site shares depth, reindent" in joined
    assert "changelog line 1 mentions 'helper'" in joined
    banned = "def f(x, cache={}):\n    if not cache:\n        pass\n"
    patch = (
        "diff --git a/p/m.py b/p/m.py\n--- a/p/m.py\n+++ b/p/m.py\n@@ -1,2 +1,2 @@\n"
        "-def f(x, cache={}):\n+def f(x, cache=None):\n     if not cache:\n"
    )
    shapes = "\n".join(site_notes(patch, {"p/m.py": banned}))
    assert "mutable default argument" in shapes


def test_find_changelog_picks_root_files_only() -> None:
    assert find_changelog(["docs/CHANGES.rst", "CHANGES.rst", "src/x.py"]) == "CHANGES.rst"
    assert find_changelog(["CHANGELOG.md"]) == "CHANGELOG.md"
    assert find_changelog(["README.md"]) is None
