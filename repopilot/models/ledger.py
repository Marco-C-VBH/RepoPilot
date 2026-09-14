"""Running totals of model usage, and the spend guard built on them.

A ``Ledger`` is attached to one agent run (or one benchmark run) and fed every
``ModelResponse``.  It is the source of the token / cost / latency numbers in
traces and summaries, and it raises ``BudgetExceeded`` the moment a cap from
the agent budget (spec §9.1) is crossed, so a runaway loop can never spend more
than the configured amount plus one call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from repopilot.models.types import ModelResponse, Usage


class BudgetExceeded(RuntimeError):
    def __init__(self, what: str, used: float, limit: float) -> None:
        self.what = what
        self.used = used
        self.limit = limit
        super().__init__(f"{what} budget exceeded: {used:g} > {limit:g}")


@dataclass
class Ledger:
    max_cost_usd: float | None = None
    max_tokens: int | None = None
    calls: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    latency_ms: int = 0
    by_model: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record(self, response: ModelResponse) -> None:
        """Add one call, then enforce the caps (the call itself already happened)."""
        self.calls += 1
        self.usage = self.usage + response.usage
        self.cost_usd += response.cost_usd
        self.latency_ms += response.latency_ms
        entry = self.by_model.setdefault(
            response.model,
            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
        )
        entry["calls"] += 1
        entry["input_tokens"] += response.usage.input_tokens
        entry["output_tokens"] += response.usage.output_tokens
        entry["cost_usd"] += response.cost_usd
        self.check()

    def check(self) -> None:
        if self.max_cost_usd is not None and self.cost_usd > self.max_cost_usd:
            raise BudgetExceeded("cost (USD)", self.cost_usd, self.max_cost_usd)
        if self.max_tokens is not None and self.usage.total_tokens > self.max_tokens:
            raise BudgetExceeded("tokens", self.usage.total_tokens, self.max_tokens)

    @property
    def remaining_cost_usd(self) -> float | None:
        if self.max_cost_usd is None:
            return None
        return max(self.max_cost_usd - self.cost_usd, 0.0)

    def to_record(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_tokens": self.usage.cache_read_tokens,
            "cache_write_tokens": self.usage.cache_write_tokens,
            "reasoning_tokens": self.usage.reasoning_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "latency_ms": self.latency_ms,
            "by_model": {
                m: {**v, "cost_usd": round(v["cost_usd"], 6)} for m, v in self.by_model.items()
            },
        }
