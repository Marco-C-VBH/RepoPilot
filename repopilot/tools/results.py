"""What every tool returns to the agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolResult:
    """``output`` is the text the model sees; ``meta`` holds structured facts for the
    trace (hit counts, files touched, test counts) that never reach the model."""

    output: str
    is_error: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def error(cls, message: str, **meta: Any) -> ToolResult:
        return cls(output=f"error: {message}", is_error=True, meta=meta)


class ToolError(ValueError):
    """A tool refused the request (bad arguments, missing file, policy); reported to
    the model as an error result, never raised through the runtime."""
