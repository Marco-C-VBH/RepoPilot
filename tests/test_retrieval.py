"""Phase 3 retrieval: chunks, the three channels, fusion, the index, the tool,
the runtime's evidence mode and the offline evaluation -- all without a model,
Docker or the local embedding model (``HashingEmbedder`` stands in)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from evals import retrieval as offline
from evals import runner
from repopilot.agent import AgentBudget, StructuredAgent, TaskInput, Termination
from repopilot.agent.context import ContextConfig
from repopilot.models import FakeClient, ModelResponse, StopReason, ToolCall, Usage
from repopilot.retrieval import (
    Chunk,
    HashingEmbedder,
    RepoIndex,
    RetrievalConfig,
    chunk_module,
    format_results,
)
from repopilot.retrieval.bm25 import BM25Index
from repopilot.retrieval.dense import DenseIndex, EmbeddingCache
from repopilot.retrieval.fusion import rrf
from repopilot.retrieval.index import snippet
from repopilot.retrieval.lexer import split_identifier, tokens
from repopilot.retrieval.metrics import (
    file_relevant,
    recall_at,
    reciprocal_rank,
    symbol_relevant,
)
from repopilot.retrieval.symbols import SymbolChannel, query_identifiers
from repopilot.tools import Toolbox, Workspace
from repopilot.tools.toolbox import RETRIEVE_SPEC
from tests.fakes import DEMO_FILES, ScriptedSandbox, make_run
from tests.gitfixtures import init_repo

CACHE_SRC = DEMO_FILES["demo/cache.py"]


def workspace(tmp_path: Path, files: dict[str, str] = DEMO_FILES) -> Workspace:
    root = tmp_path / "repo"
    init_repo(root, files)
    return Workspace.from_repo(root)


def index_for(tmp_path: Path, **kwargs) -> RepoIndex:
    return RepoIndex(workspace(tmp_path), embedder=HashingEmbedder(), cache_dir=None, **kwargs)


# ---------------------------------------------------------------------- chunks


def test_chunks_cover_module_class_methods_and_functions_without_overlap() -> None:
    chunks = chunk_module("demo/cache.py", CACHE_SRC)
    by_symbol = {c.symbol: c for c in chunks}
    assert [c.symbol_type for c in chunks] == [
        "module",
        "class",
        "method",
        "method",
        "method",
        "function",
    ]
    module = chunks[0]
    assert module.text == "DEFAULT_SIZE = 3" and module.numbers == (1,)
    assert module.defines == ("DEFAULT_SIZE", "Cache", "make_cache")
    cache = by_symbol["Cache"]
    assert (cache.start_line, cache.end_line) == (4, 15)
    assert cache.text.splitlines() == [
        "class Cache:",
        "    def __init__(self, size=DEFAULT_SIZE): ...",
        "    def put(self, key, value): ...",
        "    def get(self, key, default=None): ...",
    ]
    assert cache.numbers == (4, 5, 9, 14) and cache.defines == ("__init__", "put", "get")
    put = by_symbol["Cache.put"]
    assert (put.start_line, put.end_line, put.name) == (9, 12, "put")
    assert put.text.startswith("    def put(self, key, value):")
    assert put.numbered()[1] == (10, "        if len(self.items) > self.size:")
    make = by_symbol["make_cache"]
    assert "def build():" in make.text  # nested functions stay inside their parent
    assert make.id == "demo/cache.py:18-21" and make.title == "demo/cache.py function make_cache"
    assert not any(c.is_test for c in chunks)
    assert chunk_module("tests/test_cache.py", DEMO_FILES["tests/test_cache.py"])[0].is_test


def test_chunks_keep_imports_decorators_and_split_long_functions() -> None:
    long_body = "\n".join(f"    x{i} = {i}" for i in range(250))
    src = (
        "import os\nfrom json import loads as parse\n\n"
        "@decorator\n@another(1)\ndef big(a):\n" + long_body + "\n    return a\n"
    )
    chunks = chunk_module("pkg/big.py", src)
    assert chunks[0].symbol_type == "module" and chunks[0].imports == ("os", "parse")
    parts = [c for c in chunks if c.symbol == "big"]
    assert [p.part for p in parts] == [1, 2, 3]
    assert parts[0].start_line == 4  # the first decorator
    assert parts[0].text.startswith("@decorator")
    assert parts[-1].end_line == 4 + 3 + 250  # decorators + def + body + return
    assert sum(len(p.text.splitlines()) for p in parts) == 254
    assert parts[0].id.endswith("#1") and parts[0].text_hash() != parts[1].text_hash()


def test_unparseable_files_become_raw_windows_and_empty_files_nothing() -> None:
    chunks = chunk_module("pkg/broken.py", "def broken(:\n    pass\n")
    assert len(chunks) == 1 and chunks[0].symbol_type == "module" and chunks[0].part == 0
    assert chunks[0].text.startswith("def broken(:")
    assert chunk_module("pkg/empty.py", "") == []


# ------------------------------------------------------------------- channels


def test_lexer_splits_identifiers_and_drops_keywords() -> None:
    assert split_identifier("getSizeOf") == ["get", "Size", "Of"]
    assert split_identifier("test_missing_getsizeof") == ["test", "missing", "getsizeof"]
    assert split_identifier("HTTPServer") == ["HTTP", "Server"]
    assert tokens("def expire(self, time=None): return TTLCache.__contains__") == [
        "expire",
        "time",
        "ttlcache",
        "ttl",
        "cache",
        "__contains__",
    ]


def test_bm25_ranks_the_chunk_that_mentions_the_query_terms_first() -> None:
    chunks = chunk_module("demo/cache.py", CACHE_SRC)
    index = BM25Index([tokens(c.title + "\n" + c.text) for c in chunks])
    ranked = index.search("evicting items when the cache is over its size", n=3)
    assert chunks[ranked[0][0]].symbol == "Cache.put"
    assert index.search("", n=3) == [] and index.search("zzzz", n=3) == []


def test_dense_index_uses_the_cache_and_finds_lexical_neighbours(tmp_path: Path) -> None:
    chunks = chunk_module("demo/cache.py", CACHE_SRC)
    embedder = HashingEmbedder()
    vector = embedder.embed_query("size items pop")
    assert len(vector) == 64 and abs(sum(x * x for x in vector) - 1.0) < 1e-9
    assert embedder.embed_passages(["a b"]) == embedder.embed_passages(["a b"])  # deterministic

    cache = EmbeddingCache(tmp_path / "emb", embedder.name)
    first = DenseIndex(chunks, embedder, cache)
    assert first.embedded == len(chunks) and cache.path.exists()
    second = DenseIndex(chunks, embedder, EmbeddingCache(tmp_path / "emb", embedder.name))
    assert second.embedded == 0  # everything came from the file
    ranked = first.search("put key value items size pop", n=2)
    assert chunks[ranked[0][0]].symbol == "Cache.put"
    assert EmbeddingCache(None, "x").path is None  # memory-only cache


def test_symbol_channel_resolves_identifiers_to_definitions() -> None:
    assert query_identifiers("`TTLCache.expire` drops entries; see make_cache and Cache.put") == [
        "TTLCache.expire",
        "Cache.put",
        "make_cache",
        "drops",
        "entries",
        "see",
    ]
    chunks = chunk_module("demo/cache.py", CACHE_SRC)
    channel = SymbolChannel(chunks)
    ranked = channel.search("Cache.put keeps one item too many", n=3)
    assert [chunks[i].symbol for i, _ in ranked][:2] == ["Cache.put", "Cache"]
    plain = channel.search("the helper is wrong", n=3)
    assert plain == []  # no chunk defines "helper" in cache.py
    assert channel.search("nothing here", n=3) == []


def test_rrf_fuses_channels_and_keeps_per_channel_ranks() -> None:
    fused = rrf({"bm25": [3, 1, 2], "dense": [1, 5], "symbol": [1]})
    assert [f.index for f in fused] == [1, 3, 5, 2]  # 1/61 > 1/62 (dense #1) > 1/63
    assert fused[0].ranks == {"bm25": 2, "dense": 1, "symbol": 1}
    assert fused[1].ranks == {"bm25": 1}
    assert rrf({}) == []


# ---------------------------------------------------------------------- index


def test_repo_index_fuses_filters_tests_and_rebuilds_after_edits(tmp_path: Path) -> None:
    index = index_for(tmp_path)
    results = index.search("Cache.put keeps one item too many before evicting.", k=3)
    assert results[0].chunk.symbol == "Cache.put"
    assert set(results[0].ranks) == {"bm25", "dense", "symbol"}
    assert all(not r.chunk.is_test for r in results)
    with_tests = index.search("test_put_evicts", k=5, include_tests=True)
    assert any(r.chunk.is_test for r in with_tests)
    only_bm25 = index.search("Cache.put", k=2, channels=("bm25",))
    assert all(set(r.ranks) == {"bm25"} for r in only_bm25)

    before = index.to_record()
    index.workspace.write_text("demo/cache.py", CACHE_SRC.replace("> self.size", ">= self.size"))
    put = next(c for c in index.chunks if c.symbol == "Cache.put")
    assert ">= self.size" in put.text  # rebuilt for the new workspace version
    after = index.to_record()
    assert after["embedded"] == before["embedded"] + 1  # only the changed chunk

    with pytest.raises(ValueError, match="unknown retrieval channels"):
        RepoIndex(index.workspace, channels=("bm25", "graph"), cache_dir=None)


def test_retrieval_config_validates_and_records() -> None:
    config = RetrievalConfig(mode="evidence", embedder="hash", channels=("bm25", "symbol"))
    assert config.enabled and config.evidence
    assert config.to_record()["channels"] == ["bm25", "symbol"]
    assert not RetrievalConfig().enabled
    with pytest.raises(ValueError, match="retrieval mode"):
        RetrievalConfig(mode="always")
    with pytest.raises(ValueError, match="channels"):
        RetrievalConfig(channels=("graph",))
    with pytest.raises(ValueError, match="embedder"):
        RetrievalConfig(embedder="openai")


def test_metrics_judge_file_and_symbol_relevance() -> None:
    put = Chunk("demo/cache.py", "Cache.put", "method", 9, 12, "")
    klass = Chunk("demo/cache.py", "Cache", "class", 4, 15, "")
    other = Chunk("demo/util.py", "helper", "function", 7, 8, "")
    assert file_relevant(put, ["demo/cache.py"]) and not file_relevant(other, ["demo/cache.py"])
    assert symbol_relevant(put, ["Cache.put"]) and symbol_relevant(klass, ["Cache.put"])
    assert symbol_relevant(put, ["put"]) and not symbol_relevant(other, ["Cache.put"])
    assert recall_at([False, True, False], 2) == 1.0 and recall_at([False, False, True], 2) == 0.0
    assert reciprocal_rank([False, True]) == 0.5 and reciprocal_rank([False]) == 0.0


def test_results_render_with_line_numbers_labels_and_a_budget(tmp_path: Path) -> None:
    index = index_for(tmp_path)
    results = index.search("Cache", k=4, include_tests=True)
    text = format_results(results, snippet_lines=2, budget=100_000)
    assert text.startswith("[1] demo/cache.py:")
    assert "read_file for the rest" in text  # a chunk longer than the snippet
    assert "[test]" in text
    short = format_results(results, snippet_lines=2, budget=250)
    assert "not shown (budget)" in short and short.count("\n\n") <= 2
    klass = next(c for c in index.chunks if c.symbol == "Cache")
    assert snippet(klass, 10).splitlines()[2] == "    9|     def put(self, key, value): ..."
    assert format_results([], snippet_lines=2, budget=100) == "no matching code found"


# ----------------------------------------------------------------------- tool


def test_toolbox_offers_retrieve_only_with_an_index(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    plain = Toolbox(ws, None, test_command="pytest")
    assert "retrieve" not in [s.name for s in plain.specs()]
    refused = plain.call("retrieve", {"query": "x"})
    assert refused.is_error and refused.meta.get("invalid")

    index = RepoIndex(ws, embedder=HashingEmbedder(), cache_dir=None)
    toolbox = Toolbox(ws, None, test_command="pytest", retrieval=index)
    assert [s.name for s in toolbox.specs()][-1] == "retrieve"
    assert RETRIEVE_SPEC.parameters["required"] == ["query"]
    result = toolbox.call("retrieve", {"query": "pop an item when over the size", "k": 2})
    assert not result.is_error and result.output.startswith("[1] demo/cache.py:")
    # A query with no lexical or symbol overlap is an empty result, not an error (the
    # hashing embedder cannot see meaning; the local model can).
    nothing = toolbox.call("retrieve", {"query": "evicting one item too many"})
    assert not nothing.is_error and nothing.output == "no matching code found"
    assert result.meta["k"] == 2 and len(result.meta["results"]) == 2
    assert result.meta["results"][0]["ranks"] and result.meta["files"] == ["demo/cache.py"]
    assert "latency_ms" in result.meta
    bad = toolbox.call("retrieve", {"query": "x", "k": "3"})
    assert bad.is_error and "k must be an integer" in bad.output
    empty = toolbox.call("retrieve", {"query": "   "})
    assert empty.is_error and "non-empty" in empty.output


# -------------------------------------------------------------------- runtime

TASK = TaskInput(
    task_id="demo_001",
    repo="https://github.com/example/demo",
    base_commit="0123456789abcdef0123456789abcdef01234567",
    description="Cache.put keeps one item too many before evicting.",
    test_command="pytest tests/test_cache.py",
)
NODEID = "tests/test_cache.py::test_put_evicts"
PLAN = json.dumps({"plan": ["read Cache.put", "fix"], "suspects": ["demo/cache.py"]})
RETRIEVE = ToolCall("q1", "retrieve", {"query": "eviction size check", "k": 3})
EDIT = ToolCall(
    "e1",
    "edit_file",
    {
        "path": "demo/cache.py",
        "old_string": "if len(self.items) > self.size:",
        "new_string": "if len(self.items) >= self.size:",
    },
)


def response(text: str = "", calls: tuple[ToolCall, ...] = ()) -> ModelResponse:
    return ModelResponse(
        model="fake-model",
        provider="fake",
        text=text,
        tool_calls=calls,
        stop_reason=StopReason.TOOL_USE if calls else StopReason.END_TURN,
        usage=Usage(100, 10),
        latency_ms=3,
        cost_usd=0.001,
    )


def run_with(tmp_path: Path, *, mode: str, context: str):
    ws = workspace(tmp_path)
    config = RetrievalConfig(mode=mode, embedder="hash")
    toolbox = Toolbox(
        ws,
        ScriptedSandbox(
            run=make_run(**{NODEID: "passed"}),
            runs=[make_run(**{NODEID: "failed"}), make_run(**{NODEID: "passed"})],
        ),
        test_command=TASK.test_command,
        retrieval=RepoIndex(ws, embedder=HashingEmbedder(), cache_dir=None),
        retrieval_config=config,
    )
    client = FakeClient(
        script=[
            response(PLAN),  # 1 PLAN
            response("", (RETRIEVE,)),  # 2 LOCALIZE: the model's own retrieve
            response("Cache.put: > should be >=."),  # 3 hypothesis
            response("", (EDIT,)),  # 4 PATCH
            response("changed the comparison"),  # 5 -> TEST green -> DONE
        ]
    )
    agent = StructuredAgent(
        client,
        toolbox,
        AgentBudget(),
        context=ContextConfig(mode=context),
        retrieval=config,
    )
    return agent.run(TASK), client


@pytest.mark.parametrize("context", ["full", "compact"])
def test_evidence_mode_retrieves_for_the_report_and_offers_the_tool(
    tmp_path: Path, context: str
) -> None:
    run, client = run_with(tmp_path, mode="evidence", context=context)
    assert run.termination is Termination.DONE
    assert run.runtime["retrieve_calls"] == 1 and run.runtime["evidence_chars"] > 0
    assert run.runtime["retrieval"]["mode"] == "evidence"
    assert run.runtime["index"]["chunks"] == 11 and run.runtime["index"]["searches"] == 2
    # PLAN: the evidence block sits in the prompt; the tools are defined but unusable.
    plan_call = client.calls[0]
    assert "Retrieved evidence" in plan_call.messages[-1].content
    assert "[1] demo/cache.py:9-12  method  Cache.put" in plan_call.messages[-1].content
    assert plan_call.tool_choice == "none" and "retrieve" in [t.name for t in plan_call.tools]
    # LOCALIZE offers retrieve; PATCH too; the result travels like any tool result.
    assert [t.name for t in client.calls[1].tools][-1] == "retrieve"
    assert "retrieve" in [t.name for t in client.calls[3].tools]
    assert any(m.tool_call_id == "q1" for m in client.calls[2].messages)
    event = next(e for e in run.trace.events if e.kind == "retrieval")
    assert event.data["name"] == "evidence" and len(event.data["results"]) == 5
    tool_event = next(
        e for e in run.trace.events if e.kind == "tool_call" and e.data["name"] == "retrieve"
    )
    assert tool_event.data["meta"]["k"] == 3 and tool_event.data["policy"] == "executed"
    if context == "compact":
        state_at_localize = client.calls[2].messages[1].content
        state_before_edit = client.calls[3].messages[1].content  # PATCH, no patch yet
        state_after_edit = client.calls[4].messages[1].content  # PATCH, the edit is in
        assert "Retrieved evidence" in state_at_localize
        assert "Retrieved evidence" in state_before_edit  # the exact text to edit from
        assert "Retrieved evidence" not in state_after_edit  # issue #12: gone once patched
        assert "Searches made: 'eviction size check'" in state_after_edit


def test_tool_mode_offers_retrieve_without_pushing_evidence(tmp_path: Path) -> None:
    run, client = run_with(tmp_path, mode="tool", context="full")
    assert run.termination is Termination.DONE and run.runtime["evidence_chars"] == 0
    assert "Retrieved evidence" not in client.calls[0].messages[-1].content
    assert "retrieve" in [t.name for t in client.calls[1].tools]
    assert not [e for e in run.trace.events if e.kind == "retrieval"]
    assert run.runtime["retrieve_calls"] == 1


def test_runs_without_an_index_never_mention_retrieve(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    toolbox = Toolbox(
        ws,
        ScriptedSandbox(
            run=make_run(**{NODEID: "passed"}),
            runs=[make_run(**{NODEID: "failed"}), make_run(**{NODEID: "passed"})],
        ),
        test_command=TASK.test_command,
    )
    client = FakeClient(
        script=[
            response(PLAN),
            response("", (RETRIEVE,)),  # not offered: an invalid call, not a crash
            response("Cache.put: > should be >=."),
            response("", (EDIT,)),
            response("changed"),
        ]
    )
    run = StructuredAgent(client, toolbox, AgentBudget()).run(TASK)
    assert run.termination is Termination.DONE
    assert "retrieve" not in [t.name for t in client.calls[1].tools]
    assert run.invalid_tool_calls == 1 and run.runtime["retrieve_calls"] == 0
    assert run.runtime["index"] is None and run.runtime["retrieval"]["mode"] == "none"


# ------------------------------------------------------------ solver + runner


@dataclass
class FakeTask:
    id: str
    repo: str
    base_commit: str
    bug_patch: str
    description: str
    gold_files: list[str]
    gold_symbols: list[str]


def test_runner_retrieval_flags_reach_the_structured_solver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from evals.solvers import StructuredSolver
    from repopilot.models.config import ConfigError

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    parser = runner.build_parser()
    solver = runner.build_solver(
        parser.parse_args(
            [
                "--solver",
                "structured",
                "--retrieval",
                "evidence",
                "--embedder",
                "hash",
                "--channels",
                "bm25,symbol",
                "--env-file",
                "x",
            ]
        )
    )
    assert isinstance(solver, StructuredSolver)
    assert (solver.retrieval, solver.embedder, solver.channels) == (
        "evidence",
        "hash",
        ("bm25", "symbol"),
    )
    assert solver.retrieval_config().to_record()["channels"] == ["bm25", "symbol"]
    default = runner.build_solver(parser.parse_args(["--solver", "structured", "--env-file", "x"]))
    assert default.retrieval == "none"
    with pytest.raises(ConfigError, match="structured-solver option"):
        runner.build_solver(
            parser.parse_args(["--solver", "baseline", "--retrieval", "tool", "--env-file", "x"])
        )
    with pytest.raises(ConfigError, match="--channels"):
        runner.build_solver(
            parser.parse_args(
                ["--solver", "structured", "--retrieval", "tool", "--channels", "bm25,graph"]
                + ["--env-file", "x"]
            )
        )

    # make_toolbox attaches an index (and the retrieve tool) only when retrieval is on.
    ws = workspace(tmp_path)
    task = FakeTask("demo_001", "x", "0" * 40, "", TASK.description, ["demo/cache.py"], [])
    task.env = type("Env", (), {"test_timeout_seconds": 30})()  # type: ignore[attr-defined]
    task.test_command = TASK.test_command  # type: ignore[attr-defined]
    solver.cache_dir = tmp_path / "cache"
    toolbox = solver.make_toolbox(ws, None, task)  # type: ignore[arg-type]
    assert toolbox.retrieval is not None and "retrieve" in [s.name for s in toolbox.specs()]
    assert toolbox.retrieval.channels == ("bm25", "symbol")
    assert default.make_toolbox(ws, None, task).retrieval is None  # type: ignore[arg-type]


def test_offline_evaluation_scores_every_configuration(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    task = FakeTask(
        "demo_001",
        "x",
        "0" * 40,
        "",
        "Cache.put keeps one item too many before evicting.",
        ["demo/cache.py"],
        ["Cache.put"],
    )
    rows, record = offline.evaluate_workspace(task, ws, HashingEmbedder(), cache_dir=None)
    assert [r.config for r in rows] == list(offline.CONFIGS)
    assert all(r.recall10_file == 1.0 for r in rows) and record["chunks"] == 11
    fused = next(r for r in rows if r.config == "bm25+dense+symbol")
    assert fused.first_symbol_rank == 1 and fused.mrr_symbol == 1.0 and fused.top[0]["ranks"]
    summary = offline.summarize_rows(rows)
    assert summary["kind"] == "retrieval"
    assert summary["configs"]["bm25"]["recall10_file"] == 1.0
    assert summary["configs"]["bm25"]["misses_file"] == []
    summary.update(embedder="hash-64", chunks_total=11, tasks=1, embedded_total=11)
    summary["index_build_seconds_total"] = 0.0
    text = offline.format_summary(summary)
    assert text.splitlines()[1].startswith("bm25 ") and "embedder hash-64" in text

    # Bench v1 breakdowns (§9.4): the same rows grouped by the task flags.
    other = [
        offline.RetrievalRow(**{**r.to_record(), "task_id": "demo_002", "recall10_file": 0.0})
        for r in rows
    ]
    flags = {
        "demo_001": {"suite": "v0", "report_level": "internal", "cross_module": False},
        "demo_002": {"suite": "v1", "report_level": "symptom_only", "cross_module": True},
    }
    grouped = offline.summarize_rows([*rows, *other], flags=flags)
    assert set(grouped["by_group"]) == {"suite", "report_level", "cross_module"}
    assert grouped["by_group"]["suite"]["v0"]["bm25"]["recall10_file"] == 1.0
    assert grouped["by_group"]["suite"]["v1"]["bm25"]["recall10_file"] == 0.0
    assert grouped["by_group"]["cross_module"]["True"]["bm25"]["tasks"] == 1
    grouped.update(embedder="hash-64", chunks_total=11, tasks=2, embedded_total=11)
    grouped["index_build_seconds_total"] = 0.0
    assert "by suite:" in offline.format_summary(grouped)
    assert "by_group" not in offline.summarize_rows(rows, flags={"demo_001": flags["demo_001"]})

    from scripts import retrieval_eval

    args = retrieval_eval.build_parser().parse_args(["--embedder", "hash", "--k", "5"])
    assert args.embedder == "hash" and args.k == 5
