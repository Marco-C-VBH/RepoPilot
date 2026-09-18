"""The tool surface the agent sees (spec §6), and the dispatcher behind it.

``Toolbox`` binds a ``Workspace`` (host-side working copy) and a sandbox (test
executor) to six tools, plus a seventh when a retrieval index is attached:

    search_code(query, top_k)          lexical search over the repository
    search_symbol(name, kind)          where a function / class / method is defined
    find_references(symbol)            lines that use a symbol
    read_file(path, start, end)        a bounded, numbered slice of a file
    edit_file(path, old_string, new_string)   one exact replacement; tests are read-only
    run_tests(target)                  the task's test command, or one pytest target
    retrieve(query, k, include_tests)  fused BM25 + dense + symbol search (Phase 3)

``specs()`` gives the model the JSON schemas; ``call(name, arguments)`` validates
the arguments, runs the tool and always returns a ``ToolResult`` -- a model
mistake (unknown tool, missing argument, bad path) is an error *result* the model
can recover from, and is counted as an invalid tool call by the runtime.
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from repopilot.models.types import ToolSpec
from repopilot.sandbox.results import ExecResult, TestOutcome, TestRun
from repopilot.tools import code
from repopilot.tools.paths import PathError
from repopilot.tools.results import ToolResult
from repopilot.tools.workspace import Workspace, WorkspaceError

if TYPE_CHECKING:  # retrieval imports the workspace; keep the runtime import one-way
    from repopilot.retrieval.index import RepoIndex, RetrievalConfig

MAX_OUTPUT_CHARS = 6000
MAX_FAILURES_LISTED = 8
FAILURE_DETAIL_CHARS = 500


class TestSandbox(Protocol):
    """What ``run_tests`` needs from ``repopilot.sandbox.docker.Sandbox``."""

    def exec(self, command: str | Sequence[str], *, timeout: float | None = None) -> ExecResult: ...

    def apply_patch(self, patch: str, *, timeout: float = 60) -> ExecResult: ...

    def run_tests(self, test_command: str, *, timeout: float = 300) -> TestRun: ...


def _schema(properties: dict[str, dict[str, Any]], required: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "search_code",
        "Search the repository for lines containing a string (case-insensitive) or, with "
        "regex=true, matching a regular expression. Returns path:line: text for the first "
        "top_k matches, source files before tests.",
        _schema(
            {
                "query": {"type": "string", "description": "text or regex to look for"},
                "top_k": {"type": "integer", "description": "max matches (default 10, max 50)"},
                "regex": {"type": "boolean", "description": "treat query as a regex"},
            },
            ["query"],
        ),
    ),
    ToolSpec(
        "search_symbol",
        "Find where a function, method, class or module-level variable is defined. Accepts a "
        "bare name or a qualified name like Class.method. Returns path:start-end, kind and "
        "the qualified name.",
        _schema(
            {
                "name": {
                    "type": "string",
                    "description": "symbol name, e.g. expire or TTLCache.expire",
                },
                "kind": {
                    "type": "string",
                    "enum": ["function", "method", "class", "variable", "any"],
                    "description": "restrict to one kind (default any)",
                },
            },
            ["name"],
        ),
    ),
    ToolSpec(
        "find_references",
        "List the lines that mention a symbol (calls, imports, attribute access) across the "
        "repository, as path:line: text.",
        _schema(
            {
                "symbol": {"type": "string", "description": "name or Class.method"},
                "top_k": {"type": "integer", "description": "max lines (default 20, max 100)"},
            },
            ["symbol"],
        ),
    ),
    ToolSpec(
        "read_file",
        f"Read a file as numbered lines. At most {code.MAX_READ_LINES} lines per call; use "
        "start/end to page through longer files.",
        _schema(
            {
                "path": {"type": "string", "description": "repository-relative path"},
                "start": {"type": "integer", "description": "first line, 1-based (default 1)"},
                "end": {"type": "integer", "description": "last line, inclusive"},
            },
            ["path"],
        ),
    ),
    ToolSpec(
        "edit_file",
        "Replace one exact occurrence of old_string in a file with new_string. old_string "
        "must match the file text exactly once (copy it from read_file, indentation "
        "included). Test files cannot be edited.",
        _schema(
            {
                "path": {"type": "string", "description": "repository-relative path"},
                "old_string": {"type": "string", "description": "exact text to replace"},
                "new_string": {"type": "string", "description": "replacement text"},
            },
            ["path", "old_string", "new_string"],
        ),
    ),
    ToolSpec(
        "run_tests",
        "Run the task's test command in the sandbox with your current edits applied, or a "
        "narrower pytest target (a test file, directory or node id such as "
        "tests/test_x.py::test_name). Returns pass/fail counts and the failing tests' "
        "messages.",
        _schema(
            {
                "target": {
                    "type": "string",
                    "description": "pytest target(s); omit for the task's full test command",
                }
            },
            [],
        ),
    ),
)

RETRIEVE_SPEC = ToolSpec(
    "retrieve",
    "Find the code most relevant to a question or a description of behaviour: a fused "
    "lexical (BM25), semantic (embeddings) and symbol search over function-, method- and "
    "class-sized chunks of the repository. Returns the top-k chunks with their location and "
    "the first lines of each; use read_file for the rest. Best for prose queries; for an "
    "exact name use search_symbol.",
    _schema(
        {
            "query": {
                "type": "string",
                "description": "what you are looking for, in words or with identifiers",
            },
            "k": {"type": "integer", "description": "chunks to return (default 8, max 20)"},
            "include_tests": {
                "type": "boolean",
                "description": "also return chunks from test files (default false)",
            },
        },
        ["query"],
    ),
)

_SPEC_BY_NAME = {spec.name: spec for spec in TOOL_SPECS}


@dataclass
class Toolbox:
    workspace: Workspace
    sandbox: TestSandbox | None
    test_command: str
    test_timeout: float = 300
    max_output_chars: int = MAX_OUTPUT_CHARS
    retrieval: RepoIndex | None = None  # attached -> the retrieve tool exists
    retrieval_config: RetrievalConfig | None = None  # defaults when None

    def __post_init__(self) -> None:
        self._index = code.SymbolIndex(self.workspace)
        self.test_runs = 0
        self._specs = dict(_SPEC_BY_NAME)
        if self.retrieval is not None:
            self._specs[RETRIEVE_SPEC.name] = RETRIEVE_SPEC

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    # -- dispatch ---------------------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any] | None) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            return ToolResult.error(
                f"unknown tool {name!r}; available: {', '.join(self._specs)}", invalid=True
            )
        problem = _validate(spec, arguments or {})
        if problem:
            return ToolResult.error(f"{name}: {problem}", invalid=True)
        args = dict(arguments or {})
        try:
            result = self._dispatch(name, args)
        except (PathError, WorkspaceError) as exc:
            return ToolResult.error(str(exc))
        return _clip_result(result, self.max_output_chars)

    def _dispatch(self, name: str, args: dict[str, Any]) -> ToolResult:
        ws = self.workspace
        if name == "search_code":
            return code.search_code(
                ws, args["query"], top_k=args.get("top_k", 10), regex=bool(args.get("regex"))
            )
        if name == "search_symbol":
            return code.search_symbol(ws, self._index, args["name"], kind=args.get("kind"))
        if name == "find_references":
            return code.find_references(ws, args["symbol"], top_k=args.get("top_k", 20))
        if name == "read_file":
            return code.read_file(ws, args["path"], start=args.get("start", 1), end=args.get("end"))
        if name == "edit_file":
            return code.edit_file(ws, args["path"], args["old_string"], args["new_string"])
        if name == "run_tests":
            return self.run_tests(args.get("target"))
        if name == "retrieve":
            return self.retrieve(
                args["query"], k=args.get("k"), include_tests=bool(args.get("include_tests"))
            )
        raise AssertionError(name)  # pragma: no cover - guarded by self._specs

    # -- retrieve ---------------------------------------------------------------------
    def retrieve(
        self, query: str, *, k: int | None = None, include_tests: bool = False
    ) -> ToolResult:
        """The fused top-k chunks for ``query``; the result's meta carries every
        chunk's per-channel rank and the latency, for the trace."""
        from repopilot.retrieval.index import DEFAULT_RETRIEVAL, format_results

        if self.retrieval is None:
            return ToolResult.error("retrieval is not enabled for this run")
        if not isinstance(query, str) or not query.strip():
            return ToolResult.error("query must be a non-empty string")
        config = self.retrieval_config or DEFAULT_RETRIEVAL
        k = code._clamp_int(k, 1, 20, default=config.k)
        started = time.perf_counter()
        results = self.retrieval.search(query.strip(), k=k, include_tests=include_tests)
        latency_ms = int((time.perf_counter() - started) * 1000)
        output = format_results(
            results, snippet_lines=config.snippet_lines, budget=self.max_output_chars
        )
        return ToolResult(
            output,
            meta={
                "query": query.strip(),
                "k": k,
                "include_tests": include_tests,
                "channels": list(self.retrieval.channels),
                "results": [r.to_record() for r in results],
                "files": sorted({r.chunk.path for r in results}),
                "latency_ms": latency_ms,
            },
        )

    # -- run_tests --------------------------------------------------------------------
    def test_command_for(self, target: str | None) -> str:
        """The task's own command, or ``pytest <targets>`` for paths / node ids that exist."""
        if target is None or not target.strip():
            return self.test_command
        try:
            tokens = shlex.split(target)
        except ValueError as exc:
            raise PathError(f"could not parse target: {exc}") from exc
        if tokens and tokens[0] == "pytest":
            tokens = tokens[1:]
        if not tokens:
            return self.test_command
        for token in tokens:
            if token.startswith("-"):
                raise PathError(
                    f"options such as {token!r} are not allowed; give test paths or node ids"
                )
            path = token.split("::", 1)[0]
            resolved = self.workspace.resolve(path)  # rejects escapes
            if not resolved.exists():
                raise PathError(f"{path!r} does not exist in the repository")
        return "pytest " + " ".join(shlex.quote(t) for t in tokens)

    def run_tests(self, target: str | None = None) -> ToolResult:
        if self.sandbox is None:
            return ToolResult.error("no sandbox is attached; tests cannot be run here")
        try:
            command = self.test_command_for(target)
        except PathError as exc:
            return ToolResult.error(str(exc))
        self.test_runs += 1

        # Fresh copy of the buggy tree inside the container, then the agent's edits on top.
        reset = self.sandbox.exec(
            ["sh", "-c", "git reset -q --hard HEAD && git clean -fdq"], timeout=60
        )
        if not reset.ok:
            return ToolResult.error(f"could not reset the test environment: {reset.stderr.strip()}")
        patch = self.workspace.diff()
        if patch.strip():
            applied = self.sandbox.apply_patch(patch)
            if not applied.ok:
                return ToolResult.error(
                    "your edits could not be applied in the test environment: "
                    f"{(applied.stderr or applied.stdout).strip()}"
                )
        run = self.sandbox.run_tests(command, timeout=self.test_timeout)
        return _summarize_run(command, run, patch_applied=bool(patch.strip()))


# ----------------------------------------------------------------------------- helpers


def _validate(spec: ToolSpec, arguments: dict[str, Any]) -> str | None:
    """A human-readable problem with ``arguments`` against ``spec.parameters``, or None."""
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    properties = spec.parameters.get("properties", {})
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        return f"unknown argument(s) {', '.join(unknown)}; expected {', '.join(properties)}"
    missing = [key for key in spec.parameters.get("required", []) if key not in arguments]
    if missing:
        return f"missing required argument(s): {', '.join(missing)}"
    for key, value in arguments.items():
        expected = properties[key].get("type")
        if expected == "string" and not isinstance(value, str):
            return f"{key} must be a string"
        if expected == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            return f"{key} must be an integer"
        if expected == "boolean" and not isinstance(value, bool):
            return f"{key} must be true or false"
        choices = properties[key].get("enum")
        if choices and value not in choices:
            return f"{key} must be one of {', '.join(choices)}"
    return None


def _clip_result(result: ToolResult, limit: int) -> ToolResult:
    if len(result.output) <= limit:
        return result
    clipped = result.output[:limit].rstrip() + f"\n... [output truncated at {limit} characters]"
    return ToolResult(clipped, is_error=result.is_error, meta={**result.meta, "truncated": True})


def _summarize_run(command: str, run: TestRun, *, patch_applied: bool) -> ToolResult:
    counts = run.counts()
    failed = [r for r in run.tests.values() if r.outcome in (TestOutcome.FAILED, TestOutcome.ERROR)]
    failed.sort(key=lambda r: r.nodeid)
    meta: dict[str, Any] = {
        "command": command,
        "counts": counts,
        "failed": [r.nodeid for r in failed],
        "exit_code": run.exit_code,
        "timed_out": run.timed_out,
        "report_found": run.report_found,
        "duration_seconds": round(run.duration_seconds, 3),
        "patch_applied": patch_applied,
    }
    lines = [
        f"{command}: {counts['passed']} passed, {counts['failed']} failed, "
        f"{counts['error']} errors, {counts['skipped']} skipped ({run.duration_seconds:.1f}s)"
    ]
    if run.timed_out:
        lines.append("the test run TIMED OUT; look for an infinite loop or a blocking call")
    if run.collection_errors:
        lines.append("collection errors:")
        lines += [f"  {err}" for err in run.collection_errors[:MAX_FAILURES_LISTED]]
    for result in failed[:MAX_FAILURES_LISTED]:
        detail = (result.message or result.longrepr or "").strip()
        lines.append(f"{result.outcome.upper()} {result.nodeid}")
        if detail:
            lines.append("  " + _tail(detail, FAILURE_DETAIL_CHARS).replace("\n", "\n  "))
    if len(failed) > MAX_FAILURES_LISTED:
        lines.append(f"... {len(failed) - MAX_FAILURES_LISTED} more failing tests")
    if not run.report_found:
        lines.append(
            f"pytest produced no report (exit {run.exit_code}); output tail:\n"
            + _tail((run.stderr or "") + "\n" + (run.stdout or ""), 1500)
        )
    is_error = not run.report_found or run.timed_out
    return ToolResult("\n".join(lines), is_error=is_error, meta=meta)


def _tail(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]
