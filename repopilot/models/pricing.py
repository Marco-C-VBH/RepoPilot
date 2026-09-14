"""List prices used to estimate the cost of every model call.

Costs in traces and budgets are *estimates* from this table, never from the
provider's billing API, so a run's cost is known the moment the call returns
and is reproducible from the trace alone.  The table is a dated snapshot of
the vendors' public price lists; update it by hand when prices or model ids
change (``tests/test_pricing.py`` fails if a default model is missing here).

Matching is by longest prefix, so a dated id such as
``claude-haiku-4-5-20251001`` finds the ``claude-haiku-4-5`` entry.
"""

from __future__ import annotations

from dataclasses import dataclass

from repopilot.models.types import Usage

PRICES_AS_OF = "2026-09-13"


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float  # tokens served from a prompt cache
    cache_write: float  # tokens written into a prompt cache (charged on top of input)

    @classmethod
    def anthropic(cls, input: float, output: float) -> Price:
        # Cache reads are billed at 10% of the input price, cache writes at 125%
        # (5-minute cache); the write surcharge is the 25% on top of input.
        return cls(input, output, cache_read=input * 0.10, cache_write=input * 0.25)

    @classmethod
    def openai(cls, input: float, output: float) -> Price:
        # Cached input is billed at 10% of the input price; no write surcharge.
        return cls(input, output, cache_read=input * 0.10, cache_write=0.0)


PRICES: dict[str, Price] = {
    # Anthropic (platform.claude.com/docs/en/models/overview)
    "claude-fable-5-1": Price.anthropic(10.0, 50.0),
    "claude-opus-5": Price.anthropic(5.0, 25.0),
    "claude-sonnet-5": Price.anthropic(2.0, 10.0),
    "claude-haiku-4-5": Price.anthropic(1.0, 5.0),
    # OpenAI (developers.openai.com/api/docs/pricing), standard tier, short context
    "gpt-6-astra": Price.openai(10.0, 50.0),
    "gpt-5.6-sol": Price.openai(4.0, 20.0),
    "gpt-5.6-terra": Price.openai(2.0, 12.0),
    "gpt-5.6-luna": Price.openai(0.20, 1.20),
}


class UnknownModelError(LookupError):
    """No price is known for a model id; add it to ``PRICES`` before using it."""


def price_for(model: str) -> Price:
    """The price entry whose key is the longest prefix of ``model``."""
    best: str | None = None
    for key in PRICES:
        matches = model == key or model.startswith((key + "-", key + ":"))
        if matches and (best is None or len(key) > len(best)):
            best = key
    if best is None:
        raise UnknownModelError(
            f"no price known for model {model!r}; add it to repopilot/models/pricing.py"
        )
    return PRICES[best]


def is_priced(model: str) -> bool:
    try:
        price_for(model)
    except UnknownModelError:
        return False
    return True


def estimate_cost(model: str, usage: Usage) -> float:
    """USD for one call.  Cached input tokens are part of ``input_tokens``."""
    price = price_for(model)
    uncached_input = max(usage.input_tokens - usage.cache_read_tokens, 0)
    per_million = (
        uncached_input * price.input
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write
        + usage.output_tokens * price.output
    )
    return per_million / 1_000_000
