"""Structured trace of one agent run (spec §13.1).

A ``Trace`` is an append-only list of events -- run metadata, every model call
(model, tokens, latency, cost), every tool call (name, arguments, duration,
result, errors), the final patch, budget state and the termination reason.  It
is written as JSONL so a run can be replayed, aggregated or shown in a viewer
without touching the agent again.  Nothing in a trace is needed to *produce*
the verdict; the harness judges the patch on its own.

Large strings are clipped at write time so a trace stays a few hundred KB even
for long runs; the full patch is kept because it is the run's output.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TEXT_CLIP = 2000
ARGUMENT_CLIP = 800


def clip(text: str | None, limit: int = TEXT_CLIP) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + f"... [{len(text) - limit} more chars]"


def clip_arguments(arguments: dict[str, Any], limit: int = ARGUMENT_CLIP) -> dict[str, Any]:
    return {k: clip(v, limit) if isinstance(v, str) else v for k, v in arguments.items()}


@dataclass(frozen=True)
class Event:
    seq: int
    t: float  # seconds since the run started
    kind: str  # run_start | model_call | tool_call | patch | run_end
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"seq": self.seq, "t": round(self.t, 3), "kind": self.kind, **self.data})


class Trace:
    def __init__(self, *, clock: Any = time.monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self.events: list[Event] = []

    def add(self, kind: str, **data: Any) -> Event:
        event = Event(len(self.events), self._clock() - self._started, kind, data)
        self.events.append(event)
        return event

    def of_kind(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as sink:
            for event in self.events:
                sink.write(event.to_json() + "\n")
        return path

    @staticmethod
    def read(path: Path) -> list[dict[str, Any]]:
        with Path(path).open(encoding="utf-8") as source:
            return [json.loads(line) for line in source if line.strip()]
