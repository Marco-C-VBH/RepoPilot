"""Solvers: anything that turns a task into a candidate patch.

The contract is intentionally tiny.  A solver receives the task and the tag of
its already-built sandbox image, may start as many ``Sandbox`` containers as it
likes to search / read / test, and returns a unified diff against the task's
base commit (or ``None`` for "no change").  The harness then judges that diff in
a *fresh* container, so nothing a solver did to its own workspace can leak into
the verdict.

Phase 0 ships the two oracles that validate the harness itself:

* ``null`` -- changes nothing; every well-formed task must FAIL.
* ``gold`` -- returns ``task.gold_patch``; every well-formed task must PASS.

The Phase 1 baseline agent is just another entry in ``SOLVERS``.
"""

from __future__ import annotations

from typing import Protocol

from evals.benchmark.schema import Task


class Solver(Protocol):
    name: str

    def solve(self, task: Task, image: str) -> str | None: ...


class NullSolver:
    name = "null"

    def solve(self, task: Task, image: str) -> str | None:
        return None


class GoldSolver:
    name = "gold"

    def solve(self, task: Task, image: str) -> str | None:
        return task.gold_patch


SOLVERS: dict[str, type[Solver]] = {
    NullSolver.name: NullSolver,
    GoldSolver.name: GoldSolver,
}


def get_solver(name: str) -> Solver:
    try:
        return SOLVERS[name]()
    except KeyError:
        raise ValueError(f"unknown solver {name!r}; available: {sorted(SOLVERS)}") from None
