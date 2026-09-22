"""The memorization probe (docs/bench-v1-design.md §9.3).

Leak channel 5 of the audit: a widely depended-on library is in every model's
training data, so a "fix" may be recall of the original line rather than a
diagnosis.  The probe measures it directly and without tools: for every gold
symbol of every task the model is given the package, the pinned commit, the
file and the ``def``/``class`` line, and asked to write the rest from memory.
The reply is compared with the pinned source as a normalized token sequence
(comments and whitespace dropped; ``difflib.SequenceMatcher`` ratio).

For a mutation task the pinned source is the clean tree (what the fix must
restore).  For a real task the base tree is the buggy one and the fixed
version is the base plus the gold patch; both are compared, and the fixed
ratio is the number to watch for models released after the fix.

Output: one row per model × task × symbol (``results.jsonl``) and a summary
per model and suite (``summary.json``, ``kind: memorization``), archived like
any other run.
"""

from __future__ import annotations

import difflib
import io
import json
import re
import statistics
import time
import tokenize
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.benchmark.schema import Task
from repopilot.models.client import ModelClient
from repopilot.models.ledger import BudgetExceeded, Ledger
from repopilot.models.types import system, user
from repopilot.sandbox.repo import DEFAULT_CACHE_DIR
from repopilot.tools.code import SymbolIndex
from repopilot.tools.workspace import Workspace

RECALLED_AT = 0.9
PARTIAL_AT = 0.6
MAX_SOURCE_LINES = 200  # longer symbols are skipped: the probe is about lines, not files

SYSTEM_PROMPT = (
    "You reproduce Python library source code from memory. Reply with the code only: no "
    "commentary, no markdown fence, no explanation. If you do not remember it, write your "
    "best reconstruction anyway."
)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*?)\n```\s*$", re.DOTALL)


def probe_prompt(package: str, commit: str, path: str, qualname: str, signature: str) -> str:
    return (
        f"Package `{package}`, source pinned to commit {commit[:12]}. File `{path}`.\n"
        f"Write the complete definition of `{qualname}` exactly as it appears in that "
        f"version of the file, starting from this line:\n\n{signature}\n"
    )


def normalized_tokens(source: str) -> list[str]:
    """The token strings of ``source`` without comments, whitespace or layout tokens."""
    out: list[str] = []
    skip = {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
    }
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in skip:
                continue
            out.append(tok.string)
    except (tokenize.TokenError, SyntaxError, IndentationError):
        # A reply that does not tokenize is compared word by word.
        return re.findall(r"\w+|[^\w\s]", source)
    return out


def similarity(reference: str, candidate: str) -> float:
    """SequenceMatcher ratio over normalized tokens, in [0, 1]."""
    a, b = normalized_tokens(reference), normalized_tokens(candidate)
    if not a or not b:
        return 0.0
    return round(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 4)


def strip_fence(text: str) -> str:
    text = text.strip()
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text


@dataclass(frozen=True)
class SymbolSource:
    path: str
    qualname: str
    signature: str  # the def / class line
    text: str  # decorators through the last line of the body
    lines: int


def symbol_source(workspace: Workspace, qualname: str) -> SymbolSource | None:
    """The source of ``qualname`` in the workspace, or None when it is not found."""
    index = SymbolIndex(workspace)
    matches, exact = index.lookup(qualname)
    if not exact or not matches:
        return None
    symbol = matches[0]
    lines = workspace.read_text(symbol.path).splitlines()
    start, end = symbol.line, symbol.end_line
    # Include decorators directly above the definition.
    while start > 1 and lines[start - 2].lstrip().startswith("@"):
        start -= 1
    text = "\n".join(lines[start - 1 : end])
    signature = lines[symbol.line - 1]
    return SymbolSource(symbol.path, symbol.qualname, signature, text, end - start + 1)


@dataclass
class ProbeRow:
    task_id: str
    suite: str
    source: str
    repo: str
    model: str
    symbol: str
    path: str
    lines: int
    ratio: float  # against the pinned (base) tree
    ratio_fixed: float | None  # real tasks: against the base + gold patch
    exact: bool
    recalled: bool
    partial: bool
    cost_usd: float
    reply_chars: int

    def to_record(self) -> dict[str, Any]:
        return dict(self.__dict__)


def probe_task(
    task: Task,
    client: ModelClient,
    *,
    ledger: Ledger | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    max_tokens: int = 4000,
) -> list[ProbeRow]:
    """One model call per gold symbol of ``task``; rows for the ones that exist."""
    rows: list[ProbeRow] = []
    with Workspace.create(task.repo, task.base_commit, None, cache_dir=cache_dir) as base:
        fixed: Workspace | None = None
        try:
            if task.source == "real":
                fixed = Workspace.create(
                    task.repo, task.base_commit, task.gold_patch, cache_dir=cache_dir
                )
            for qualname in task.gold_symbols:
                found = symbol_source(base, qualname)
                if found is None or found.lines > MAX_SOURCE_LINES:
                    continue
                prompt = probe_prompt(
                    task.repo_name, task.base_commit, found.path, found.qualname, found.signature
                )
                response = client.complete(
                    [system(SYSTEM_PROMPT), user(prompt)], max_tokens=max_tokens, temperature=0.0
                )
                if ledger is not None:
                    ledger.record(response)
                reply = strip_fence(response.text)
                ratio = similarity(found.text, reply)
                ratio_fixed = None
                if fixed is not None:
                    fixed_source = symbol_source(fixed, qualname)
                    if fixed_source is not None:
                        ratio_fixed = similarity(fixed_source.text, reply)
                rows.append(
                    ProbeRow(
                        task_id=task.id,
                        suite=str(task.suite),
                        source=str(task.source),
                        repo=task.repo_name,
                        model=client.model,
                        symbol=found.qualname,
                        path=found.path,
                        lines=found.lines,
                        ratio=ratio,
                        ratio_fixed=ratio_fixed,
                        exact=normalized_tokens(found.text) == normalized_tokens(reply),
                        recalled=ratio >= RECALLED_AT,
                        partial=PARTIAL_AT <= ratio < RECALLED_AT,
                        cost_usd=round(response.cost_usd, 6),
                        reply_chars=len(reply),
                    )
                )
        finally:
            if fixed is not None:
                fixed.close()
    return rows


def summarize(rows: Sequence[ProbeRow]) -> dict[str, Any]:
    by_model: dict[str, dict[str, Any]] = {}
    for model in sorted({r.model for r in rows}):
        mine = [r for r in rows if r.model == model]
        buckets: dict[str, Any] = {}
        for key in ("all", *sorted({r.suite for r in mine}), *sorted({r.repo for r in mine})):
            subset = [r for r in mine if key == "all" or r.suite == key or r.repo == key]
            if not subset:
                continue
            buckets[key] = {
                "symbols": len(subset),
                "recalled": sum(r.recalled for r in subset),
                "partial": sum(r.partial for r in subset),
                "exact": sum(r.exact for r in subset),
                "recalled_rate": round(sum(r.recalled for r in subset) / len(subset), 4),
                "ratio_median": round(statistics.median(r.ratio for r in subset), 4),
            }
        real = [r for r in mine if r.ratio_fixed is not None]
        if real:
            buckets["real_fixed"] = {
                "symbols": len(real),
                "recalled_fixed": sum(r.ratio_fixed >= RECALLED_AT for r in real),  # type: ignore[operator]
                "ratio_fixed_median": round(
                    statistics.median(r.ratio_fixed for r in real),  # type: ignore[type-var]
                    4,
                ),
            }
        by_model[model] = {
            **buckets,
            "cost_usd": round(sum(r.cost_usd for r in mine), 4),
        }
    return {
        "kind": "memorization",
        "recalled_at": RECALLED_AT,
        "partial_at": PARTIAL_AT,
        "by_model": by_model,
    }


def run_probe(
    tasks: Sequence[Task],
    clients: Sequence[ModelClient],
    *,
    out_dir: Path | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    max_cost_usd: float | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    t0 = time.monotonic()
    ledger = Ledger(max_cost_usd=max_cost_usd)
    rows: list[ProbeRow] = []
    stopped: str | None = None
    try:
        for client in clients:
            for task in tasks:
                task_rows = probe_task(task, client, ledger=ledger, cache_dir=cache_dir)
                rows.extend(task_rows)
                for row in task_rows:
                    log(
                        f"{client.model:<28} {task.id:<18} {row.symbol:<36} ratio {row.ratio:.2f}"
                        + (f" fixed {row.ratio_fixed:.2f}" if row.ratio_fixed is not None else "")
                        + (" recalled" if row.recalled else " partial" if row.partial else "")
                    )
    except BudgetExceeded as exc:
        stopped = str(exc)
        log(f"stopped: {exc}")
    summary = summarize(rows)
    summary.update(
        {
            "tasks": len(tasks),
            "models": [c.model for c in clients],
            "started_at": started_at,
            "duration_seconds": round(time.monotonic() - t0, 1),
            "cost_total_usd": round(ledger.cost_usd, 4),
            "stopped_early": stopped,
        }
    )
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        with (out_dir / "results.jsonl").open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row.to_record()) + "\n")
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        summary["results_dir"] = str(out_dir)
    return summary


def format_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"{'model':<28} {'scope':<12} {'symbols':>7} {'recalled':>8} {'partial':>7} "
        f"{'exact':>5} {'ratio p50':>9}"
    ]
    for model, buckets in summary["by_model"].items():
        for scope, b in buckets.items():
            if scope in ("cost_usd", "real_fixed"):
                continue
            lines.append(
                f"{model:<28} {scope:<12} {b['symbols']:>7} {b['recalled']:>8} {b['partial']:>7} "
                f"{b['exact']:>5} {b['ratio_median']:>9.2f}"
            )
        real = buckets.get("real_fixed")
        if real:
            lines.append(
                f"{model:<28} {'real: fixed':<12} {real['symbols']:>7} "
                f"{real['recalled_fixed']:>8} {'':>7} {'':>5} {real['ratio_fixed_median']:>9.2f}"
            )
        lines.append(f"{model:<28} cost ${buckets['cost_usd']:.3f}")
    lines.append(
        f"recalled = ratio >= {summary['recalled_at']}, partial = ratio in "
        f"[{summary['partial_at']}, {summary['recalled_at']}); ratio = SequenceMatcher over "
        "normalized tokens against the pinned source"
    )
    return "\n".join(lines)
