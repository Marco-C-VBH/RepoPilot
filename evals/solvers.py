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

from evals.benchmark.audit import GENERIC_REPORT, SymbolTable, redact
from evals.benchmark.schema import Task
from repopilot.agent.baseline import BaselineAgent
from repopilot.agent.budget import DEFAULT_BUDGET, AgentBudget
from repopilot.agent.context import ContextConfig
from repopilot.agent.policies import DEFAULT_LIMITS as DEFAULT_RUNTIME_LIMITS
from repopilot.agent.policies import RuntimeLimits
from repopilot.agent.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_NO_TESTS, TaskInput
from repopilot.agent.run import AgentRun
from repopilot.agent.runtime import StructuredAgent
from repopilot.models.client import ModelClient, client_for
from repopilot.models.config import DEFAULT_STRONG_MODEL
from repopilot.retrieval.dense import Embedder
from repopilot.retrieval.index import CHANNELS, RepoIndex, RetrievalConfig, make_embedder
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


REPORT_MODES = ("full", "redacted", "generic")


@dataclass
class AgentSolver:
    """Run an agent on a host workspace + sandbox and return its diff.

    Subclasses pick the agent; everything else (workspace, sandbox, toolbox,
    budget, model client) is identical, so a comparison between solvers is a
    comparison between agents and nothing else.

    ``report`` is the leak ablation's report switch (docs/bench-v1-design.md
    §9.2): ``full`` shows the task's bug report, ``redacted`` the report with
    every repository symbol replaced by a placeholder (arm B′), ``generic``
    a one-line "there is a bug" (arm D).
    """

    name = "agent"
    model: str = DEFAULT_STRONG_MODEL
    budget: AgentBudget = DEFAULT_BUDGET
    cache_dir: Path = DEFAULT_CACHE_DIR
    limits: SandboxLimits = DEFAULT_LIMITS
    client_factory: Callable[[str], ModelClient] = client_for
    report: str = "full"
    last_run: AgentRun | None = field(default=None, init=False, repr=False)
    last_report: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.report not in REPORT_MODES:
            raise ValueError(f"report must be one of {REPORT_MODES}, not {self.report!r}")

    def make_agent(self, client: ModelClient, toolbox: Toolbox) -> Agent:
        raise NotImplementedError

    def make_toolbox(self, workspace: Workspace, sandbox: Sandbox, task: Task) -> Toolbox:
        return Toolbox(
            workspace,
            sandbox,
            test_command=task.test_command,
            test_timeout=task.env.test_timeout_seconds,
        )

    def report_text(self, task: Task, workspace: Workspace) -> str:
        """The bug report the agent sees under ``self.report``."""
        if self.report == "generic":
            return GENERIC_REPORT
        if self.report == "redacted":
            table = SymbolTable.from_workspace(workspace)
            return redact(task.description, table, gold_files=task.gold_files)
        return task.description

    def solve(self, task: Task, image: str) -> str | None:
        self.last_run = None
        self.last_report = None
        client = self.client_factory(self.model)
        with (
            Workspace.create(
                task.repo, task.base_commit, task.bug_patch, cache_dir=self.cache_dir
            ) as workspace,
            Sandbox(image, self.limits) as sandbox,
        ):
            toolbox = self.make_toolbox(workspace, sandbox, task)
            agent = self.make_agent(client, toolbox)
            description = self.report_text(task, workspace)
            self.last_report = description
            run = agent.run(
                TaskInput(
                    task_id=task.id,
                    repo=task.repo,
                    base_commit=task.base_commit,
                    description=description,
                    test_command=task.test_command,
                )
            )
        self.last_run = run
        return run.patch or None


@dataclass
class BaselineSolver(AgentSolver):
    """Phase 1: ``BaselineAgent``, the unconstrained tool loop.

    ``run_tests=False`` withholds the ``run_tests`` tool (leak ablation arms C
    and D: the agent cannot see which tests fail); the harness still judges
    the patch with the full suite.
    """

    name = "baseline"
    run_tests: bool = True

    def make_toolbox(self, workspace: Workspace, sandbox: Sandbox, task: Task) -> Toolbox:
        toolbox = super().make_toolbox(workspace, sandbox, task)
        if not self.run_tests:
            toolbox.disabled_tools = ("run_tests",)
            toolbox.__post_init__()
        return toolbox

    def make_agent(self, client: ModelClient, toolbox: Toolbox) -> Agent:
        prompt = SYSTEM_PROMPT if self.run_tests else SYSTEM_PROMPT_NO_TESTS
        return BaselineAgent(client, toolbox, self.budget, system_prompt=prompt)


@dataclass
class StructuredSolver(AgentSolver):
    """Phase 2: ``StructuredAgent``, the state-machine runtime.

    ``context`` selects the context strategy (spec §12.3): ``full`` replays the
    conversation, ``compact`` rebuilds each prompt from the working state.
    ``retrieval`` (Phase 3, spec §7): ``none``, ``tool`` (the model may call
    ``retrieve``) or ``evidence`` (the tool, plus the report's top chunks shown
    at PLAN); ``embedder`` and ``channels`` configure the index.  One embedder
    is shared across the solver's tasks, so the local model loads once.
    """

    name = "structured"
    runtime_limits: RuntimeLimits = DEFAULT_RUNTIME_LIMITS
    context: str = "full"
    retrieval: str = "none"
    embedder: str = "local"
    channels: tuple[str, ...] = CHANNELS
    _embedder: Embedder | None = field(default=None, init=False, repr=False)

    def retrieval_config(self) -> RetrievalConfig:
        return RetrievalConfig(
            mode=self.retrieval, channels=tuple(self.channels), embedder=self.embedder
        )

    def make_toolbox(self, workspace: Workspace, sandbox: Sandbox, task: Task) -> Toolbox:
        toolbox = super().make_toolbox(workspace, sandbox, task)
        config = self.retrieval_config()
        if config.enabled:
            if self._embedder is None:
                self._embedder = make_embedder(config.embedder)
            toolbox.retrieval = RepoIndex(
                workspace,
                channels=config.channels,
                embedder=self._embedder,
                per_channel=config.per_channel,
                cache_dir=self.cache_dir,
            )
            toolbox.retrieval_config = config
            toolbox.__post_init__()  # the retrieve tool joins the specs
            # Build (and embed) before the agent's clock starts: the index is part
            # of the environment, not of the run's 600 s.
            len(toolbox.retrieval.chunks)
        return toolbox

    def make_agent(self, client: ModelClient, toolbox: Toolbox) -> Agent:
        return StructuredAgent(
            client,
            toolbox,
            self.budget,
            limits=self.runtime_limits,
            context=ContextConfig(mode=self.context),
            retrieval=self.retrieval_config(),
        )


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
