"""Solvers: anything that turns a task into a candidate patch.

The contract is intentionally tiny.  A solver receives the task and the tag of
its already-built sandbox image, may start as many ``Sandbox`` containers as it
likes to search / read / test, and returns a unified diff against the task's
base commit (or ``None`` for "no change").  The harness then judges that diff in
a *fresh* container, so nothing a solver did to its own workspace can leak into
the verdict.

* ``null`` -- changes nothing; every well-formed task must FAIL.
* ``gold`` -- returns ``task.gold_patch``; every well-formed task must PASS.
* ``baseline`` -- the Phase 1 agent: a plain tool-calling loop under a budget
  (``repopilot.agent.baseline``).  It exposes ``last_run`` so the harness can
  store the run's metrics and trace next to the verdict.
* ``structured`` -- the Phase 2 runtime (``repopilot.agent.runtime``): the same
  tools, workspace and sandbox, driven by a state machine.  Same options, same
  ``last_run``; the two are the arms of the architecture ablation (spec §12.2).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from evals.benchmark.schema import Task
from repopilot.agent.baseline import BaselineAgent
from repopilot.agent.budget import DEFAULT_BUDGET, AgentBudget
from repopilot.agent.policies import DEFAULT_LIMITS as DEFAULT_RUNTIME_LIMITS
from repopilot.agent.policies import RuntimeLimits
from repopilot.agent.prompts import TaskInput
from repopilot.agent.run import AgentRun
from repopilot.agent.runtime import StructuredAgent
from repopilot.models.client import ModelClient, client_for
from repopilot.models.config import DEFAULT_STRONG_MODEL
from repopilot.sandbox.docker import Sandbox
from repopilot.sandbox.limits import DEFAULT_LIMITS, SandboxLimits
from repopilot.sandbox.repo import DEFAULT_CACHE_DIR
from repopilot.tools.toolbox import Toolbox
from repopilot.tools.workspace import Workspace


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


class Agent(Protocol):
    def run(self, task: TaskInput) -> AgentRun: ...


@dataclass
class AgentSolver:
    """Run an agent on a host workspace + sandbox and return its diff.

    Subclasses pick the agent; everything else (workspace, sandbox, toolbox,
    budget, model client) is identical, so a comparison between solvers is a
    comparison between agents and nothing else.
    """

    name = "agent"
    model: str = DEFAULT_STRONG_MODEL
    budget: AgentBudget = DEFAULT_BUDGET
    cache_dir: Path = DEFAULT_CACHE_DIR
    limits: SandboxLimits = DEFAULT_LIMITS
    client_factory: Callable[[str], ModelClient] = client_for
    last_run: AgentRun | None = field(default=None, init=False, repr=False)

    def make_agent(self, client: ModelClient, toolbox: Toolbox) -> Agent:
        raise NotImplementedError

    def solve(self, task: Task, image: str) -> str | None:
        self.last_run = None
        client = self.client_factory(self.model)
        with (
            Workspace.create(
                task.repo, task.base_commit, task.bug_patch, cache_dir=self.cache_dir
            ) as workspace,
            Sandbox(image, self.limits) as sandbox,
        ):
            toolbox = Toolbox(
                workspace,
                sandbox,
                test_command=task.test_command,
                test_timeout=task.env.test_timeout_seconds,
            )
            agent = self.make_agent(client, toolbox)
            run = agent.run(
                TaskInput(
                    task_id=task.id,
                    repo=task.repo,
                    base_commit=task.base_commit,
                    description=task.description,
                    test_command=task.test_command,
                )
            )
        self.last_run = run
        return run.patch or None


@dataclass
class BaselineSolver(AgentSolver):
    """Phase 1: ``BaselineAgent``, the unconstrained tool loop."""

    name = "baseline"

    def make_agent(self, client: ModelClient, toolbox: Toolbox) -> Agent:
        return BaselineAgent(client, toolbox, self.budget)


@dataclass
class StructuredSolver(AgentSolver):
    """Phase 2: ``StructuredAgent``, the state-machine runtime."""

    name = "structured"
    runtime_limits: RuntimeLimits = DEFAULT_RUNTIME_LIMITS

    def make_agent(self, client: ModelClient, toolbox: Toolbox) -> Agent:
        return StructuredAgent(client, toolbox, self.budget, limits=self.runtime_limits)


SOLVERS: dict[str, type[Any]] = {
    NullSolver.name: NullSolver,
    GoldSolver.name: GoldSolver,
    BaselineSolver.name: BaselineSolver,
    StructuredSolver.name: StructuredSolver,
}
AGENT_SOLVERS = (BaselineSolver.name, StructuredSolver.name)


def get_solver(name: str, **options: Any) -> Solver:
    """Instantiate a solver by name; ``options`` go to solvers that take them."""
    try:
        cls = SOLVERS[name]
    except KeyError:
        raise ValueError(f"unknown solver {name!r}; available: {sorted(SOLVERS)}") from None
    if issubclass(cls, AgentSolver):
        return cls(**options)
    if options:
        raise ValueError(f"solver {name!r} takes no options (got {sorted(options)})")
    return cls()
