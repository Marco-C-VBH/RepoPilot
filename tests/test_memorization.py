"""The memorization probe (evals/memorization.py) with a scripted model and the fixture repo."""

from __future__ import annotations

import json
from pathlib import Path

from evals.benchmark.schema import Task
from evals.memorization import (
    format_summary,
    normalized_tokens,
    probe_prompt,
    run_probe,
    similarity,
    strip_fence,
    summarize,
    symbol_source,
)
from repopilot.models.client import FakeClient
from repopilot.tools.workspace import Workspace
from tests.fixture_repo import create_fixture_repo, fixture_task_dict

CLAMP = (
    "def clamp(value, low, high):\n"
    '    """Clamp value into [low, high]."""\n'
    "    return max(low, min(value, high))\n"
)


def test_similarity_ignores_comments_and_layout_but_not_code() -> None:
    assert similarity(CLAMP, CLAMP) == 1.0
    assert similarity(CLAMP, CLAMP.replace("    return", "    # note\n    return")) == 1.0
    assert similarity(CLAMP, CLAMP.replace("high))", "high - 1))")) < 1.0
    assert similarity(CLAMP, "def other(): pass") < 0.5
    assert similarity("", CLAMP) == 0.0
    assert normalized_tokens("x = 1  # c\n") == ["x", "=", "1"]
    assert normalized_tokens("def (:\n  broken") != []  # untokenizable text falls back to words


def test_strip_fence_and_prompt() -> None:
    assert strip_fence("```python\nx = 1\n```\n") == "x = 1"
    assert strip_fence("x = 1") == "x = 1"
    prompt = probe_prompt("click", "0" * 40, "src/click/core.py", "Option.__init__", "def f():")
    assert "commit 000000000000" in prompt and "`Option.__init__`" in prompt
    assert prompt.rstrip().endswith("def f():")


def test_symbol_source_includes_decorators(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    create_fixture_repo(repo)
    (repo / "fixturepkg" / "deco.py").write_text(
        "import functools\n\n\n@functools.cache\ndef cached(x):\n    return x\n"
    )
    ws = Workspace.from_repo(repo)
    found = symbol_source(ws, "cached")
    assert found is not None
    assert found.path == "fixturepkg/deco.py" and found.signature == "def cached(x):"
    assert found.text.startswith("@functools.cache\ndef cached") and found.lines == 3
    assert symbol_source(ws, "missing") is None


def test_run_probe_scores_replies_and_summarizes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sha = create_fixture_repo(repo)
    task = Task.model_validate(fixture_task_dict(repo, sha))
    recalled = FakeClient(model="recaller", script=["```python\n" + CLAMP + "```"])
    guesser = FakeClient(
        model="guesser", script=["def clamp(value, low, high):\n    return value\n"]
    )
    summary = run_probe(
        [task], [recalled, guesser], out_dir=tmp_path / "out", cache_dir=tmp_path / "cache"
    )
    lines = (tmp_path / "out" / "results.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    assert [r["model"] for r in rows] == ["recaller", "guesser"]
    assert rows[0]["ratio"] == 1.0 and rows[0]["recalled"] and rows[0]["exact"]
    assert rows[1]["ratio"] < 0.9 and not rows[1]["recalled"]
    assert rows[0]["ratio_fixed"] is None  # a mutation task: one reference
    by = summary["by_model"]
    assert by["recaller"]["all"]["recalled"] == 1 and by["guesser"]["all"]["recalled"] == 0
    assert by["recaller"]["v0"]["symbols"] == 1 and by["recaller"]["repo"]["symbols"] == 1
    assert summary["kind"] == "memorization" and summary["models"] == ["recaller", "guesser"]
    text = format_summary(summary)
    assert "recaller" in text and "guesser" in text
    assert recalled.calls[0].temperature == 0.0 and not recalled.calls[0].tools


def test_summarize_handles_no_rows() -> None:
    assert summarize([])["by_model"] == {}
