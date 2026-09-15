"""The Phase 2 structured runtime: a state machine that owns control (spec §5).

    INITIALIZE  runtime  reproduce: run the task's tests once, before any change
    PLAN        model    no tools; plan + suspects as JSON
    LOCALIZE    model    search / read; a reply without tool calls is the hypothesis
    PATCH       model    edit; a reply without tool calls and a changed diff -> TEST
    TEST        runtime  the full test command; green -> DONE (or FINALIZE when the
                         suite was green from the start), otherwise -> ANALYZE
    ANALYZE     model    no tools; JSON: diagnosis, next phase, keep or revert the patch
    FINALIZE    model    no tools; JSON: done or continue (once)

The model decides *within* phases; the runtime decides the transitions, runs the
tests, validates every call, refuses tools outside the phase, detects repeated
calls (spec §9.2), asks for the hypothesis when a phase's tool budget is spent,
nudges once when a turn ends without the edit that was due, and terminates
deterministically on success or on any budget.  The conversation history is
kept in full, exactly as the baseline does: Phase 2a changes control only, so
the architecture ablation (spec §12.2) isolates it.  See docs/runtime-design.md.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from repopilot.agent.baseline import DEFAULT_MAX_OUTPUT_TOKENS
from repopilot.agent.budget import DEFAULT_BUDGET, AgentBudget, BudgetTracker
from repopilot.agent.policies import (
    DEFAULT_LIMITS,
    PHASE_TOOLS,
    LoopDetector,
    RuntimeLimits,
    parse_json_reply,
    phase_refusal,
    string_list,
)
from repopilot.agent.prompts import (
    EMPTY_REPLY_NUDGE,
    FINALIZE_INSTRUCTIONS,
    HYPOTHESIS_REQUEST,
    LOCALIZE_INSTRUCTIONS,
    NO_EDIT_NUDGE,
    PATCH_INSTRUCTIONS,
    RUNTIME_SYSTEM_PROMPT,
    TaskInput,
    analyze_prompt,
    loop_notice,
    phase_limit_notice,
    plan_prompt,
)
from repopilot.agent.run import BUDGET_TERMINATION, AgentRun, Termination
from repopilot.agent.state import AgentState, Hypothesis, Phase, TestSummary
from repopilot.models.client import ModelClient, ModelError
from repopilot.models.ledger import Ledger
from repopilot.models.types import (
    Message,
    ModelResponse,
    StopReason,
    ToolCall,
    ToolSpec,
    system,
    tool_result,
    user,
)
from repopilot.tools.results import ToolResult
from repopilot.tools.toolbox import Toolbox
from repopilot.tracing.events import Trace, clip, clip_arguments

MODEL_TOOLS = ("search_code", "search_symbol", "find_references", "read_file", "edit_file")


class StructuredAgent:
    def __init__(
        self,
        client: ModelClient,
        toolbox: Toolbox,
        budget: AgentBudget = DEFAULT_BUDGET,
        *,
        limits: RuntimeLimits = DEFAULT_LIMITS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        system_prompt: str = RUNTIME_SYSTEM_PROMPT,
    ) -> None:
        self.client = client
        self.toolbox = toolbox
        self.budget = budget
        self.limits = limits
        self.max_output_tokens = max_output_tokens
        self.system_prompt = system_prompt

    def run(self, task: TaskInput) -> AgentRun:
        return _Execution(self, task).run()


class _Execution:
    """One run: the messages, the state, the trace and the phase methods."""

    def __init__(self, agent: StructuredAgent, task: TaskInput) -> None:
        self.agent = agent
        self.task = task
        self.client = agent.client
        self.toolbox = agent.toolbox
        self.limits = agent.limits
        self.budget = agent.budget
        self.trace = Trace()
        self.tracker = BudgetTracker(agent.budget, Ledger())
        self.state = AgentState(task.task_id)
        self.loops = LoopDetector(agent.limits)
        self.specs = {spec.name: spec for spec in self.toolbox.specs()}
        self.messages: list[Message] = [system(agent.system_prompt)]
        self.invalid_tool_calls = 0
        self.files_read: list[str] = []
        self.files_edited: list[str] = []
        self.final_text = ""
        self.last_test_output = ""

    # -- the state machine ----------------------------------------------------------
    def run(self) -> AgentRun:
        self.trace.add(
            "run_start",
            task_id=self.task.task_id,
            model=self.client.model,
            provider=self.client.provider,
            budget=self.budget.to_record(),
            limits=self.limits.to_record(),
            tools=list(MODEL_TOOLS),
            runtime="structured",
        )
        self.initialize()
        termination = self.plan()
        if termination:
            return self.finish(termination)

        next_phase = "localize"
        reason: str | None = None
        while True:
            if next_phase == "localize":
                termination = self.localize(reason)
                if termination:
                    return self.finish(termination)
                next_phase, reason = "patch", None
            elif next_phase == "patch":
                termination = self.patch(reason)
                if termination:
                    return self.finish(termination)
                next_phase, reason = "test", None
            elif next_phase == "test":
                outcome = self.test()
                if isinstance(outcome, Termination):
                    return self.finish(outcome, "a changed patch could not be verified")
                next_phase, reason = outcome, None
            elif next_phase == "analyze":
                outcome = self.analyze()
                if isinstance(outcome, Termination):
                    return self.finish(outcome)
                next_phase, reason = outcome
            elif next_phase == "finalize":
                outcome = self.finalize()
                if isinstance(outcome, Termination):
                    return self.finish(outcome)
                next_phase, reason = outcome
            else:  # done
                return self.finish(Termination.DONE, "verified: the full test command passed")

    # -- phases -----------------------------------------------------------------------
    def initialize(self) -> None:
        self.enter(Phase.INITIALIZE)
        summary = self.run_tests("reproduce")
        if summary is None:
            self.last_test_output = "not run (no test-run budget)"
        elif not summary.is_error:
            self.state.initial_failures = list(summary.still_failing)

    def plan(self) -> Termination | None:
        self.enter(Phase.PLAN)
        self.messages.append(
            user(
                plan_prompt(
                    self.task, self.last_test_output, suite_green=self.state.suite_was_green
                )
            )
        )
        response = self.model_call(())
        if isinstance(response, Termination):
            return response
        parsed = parse_json_reply(response.text) or {}
        self.state.plan = string_list(parsed.get("plan"))
        self.state.suspects = string_list(parsed.get("suspects"))
        if not self.state.plan and response.text.strip():
            self.state.plan = [clip(response.text.strip(), 400)]
        self.trace.add(
            "decision",
            step=self.tracker.steps,
            name="plan",
            parsed=bool(parsed),
            plan=self.state.plan,
            suspects=self.state.suspects,
        )
        return None

    def localize(self, reason: str | None) -> Termination | None:
        self.enter(Phase.LOCALIZE, reason)
        allowed = PHASE_TOOLS[Phase.LOCALIZE]
        self.messages.append(user(_with_note(LOCALIZE_INSTRUCTIONS, reason)))
        calls_this_visit = 0
        nudged = False
        forced = False
        while True:
            tools: Sequence[ToolSpec]
            if calls_this_visit >= self.limits.localize_tool_calls and not forced:
                forced = True
                self.state.forced_transitions += 1
                self.intervention("hypothesis_requested", calls=calls_this_visit)
                self.messages.append(user(HYPOTHESIS_REQUEST))
            tools = () if forced else [self.specs[name] for name in allowed]
            response = self.model_call(tools)
            if isinstance(response, Termination):
                return response
            if response.tool_calls:
                calls_this_visit += len(response.tool_calls)
                termination, _ = self.execute_calls(response.tool_calls, allowed)
                if termination or (termination := self.spent()):
                    return termination
                continue
            if response.stop_reason is StopReason.OTHER:
                return Termination.MODEL_STOPPED
            text = response.text.strip()
            if not text and not nudged:
                nudged = True
                self.nudge(EMPTY_REPLY_NUDGE)
                continue
            hypothesis = Hypothesis(text or "(no hypothesis stated)", self.tracker.steps)
            self.state.hypotheses.append(hypothesis)
            self.trace.add(
                "decision", step=self.tracker.steps, name="hypothesis", text=clip(text, 600)
            )
            return None

    def patch(self, reason: str | None) -> Termination | None:
        self.enter(Phase.PATCH, reason)
        allowed = PHASE_TOOLS[Phase.PATCH]
        self.messages.append(user(_with_note(PATCH_INSTRUCTIONS, reason)))
        tested = self.state.patches_tested[-1] if self.state.patches_tested else ""
        edits_this_visit = 0
        nudged = False
        while True:
            response = self.model_call([self.specs[name] for name in allowed])
            if isinstance(response, Termination):
                return response
            if response.tool_calls:
                edit_cap = self.limits.patch_edits - edits_this_visit
                termination, edits = self.execute_calls(response.tool_calls, allowed, edit_cap)
                edits_this_visit += edits
                if termination or (termination := self.spent()):
                    return termination
                continue
            if response.stop_reason is StopReason.OTHER:
                return Termination.MODEL_STOPPED
            diff = self.toolbox.workspace.diff()
            if diff.strip() and diff != tested:
                return None
            if not nudged:
                nudged = True
                self.nudge(NO_EDIT_NUDGE)
                continue
            return Termination.NO_PROGRESS

    def test(self) -> str | Termination:
        self.enter(Phase.TEST)
        summary = self.run_tests("verify")
        if summary is None:
            return Termination.BUDGET_TEST_RUNS
        self.state.patches_tested.append(self.toolbox.workspace.diff())
        if summary.green:
            if self.state.suite_was_green:
                return "finalize"  # green proves nothing here: the suite started green
            if self.state.first_green_step is None:
                self.state.first_green_step = self.tracker.steps
            return "done"
        return "analyze"

    def analyze(self) -> tuple[str, str | None] | Termination:
        self.enter(Phase.ANALYZE)
        self.state.analyze_rounds += 1
        active = self.state.active_hypothesis
        if active is not None:
            active.status = "tested"
        self.messages.append(user(analyze_prompt(self.last_test_output)))
        response = self.model_call(())
        if isinstance(response, Termination):
            return response
        parsed = parse_json_reply(response.text) or {}
        next_phase = parsed.get("next") if parsed.get("next") in ("patch", "localize") else "patch"
        keep_patch = parsed.get("keep_patch") is not False
        diagnosis = str(parsed.get("diagnosis") or clip(response.text.strip(), 400))
        self.trace.add(
            "decision",
            step=self.tracker.steps,
            name="analyze",
            parsed=bool(parsed),
            next=next_phase,
            keep_patch=keep_patch,
            diagnosis=clip(diagnosis, 600),
        )
        note = None
        if not keep_patch:
            self.toolbox.workspace.reset()
            self.state.workspace_resets += 1
            self.intervention("workspace_reset")
            note = "Your change has been reverted; the repository is back to its original state."
        if next_phase == "localize" and active is not None:
            active.status = "rejected"
        return next_phase, note

    def finalize(self) -> tuple[str, str | None] | Termination:
        self.enter(Phase.FINALIZE)
        self.messages.append(user(FINALIZE_INSTRUCTIONS))
        response = self.model_call(())
        if isinstance(response, Termination):
            return response
        parsed = parse_json_reply(response.text) or {}
        decision = "continue" if parsed.get("decision") == "continue" else "done"
        self.trace.add(
            "decision",
            step=self.tracker.steps,
            name="finalize",
            parsed=bool(parsed),
            decision=decision,
            reason=clip(str(parsed.get("reason") or ""), 400),
        )
        if (
            decision == "continue"
            and self.state.finalize_continues < self.limits.finalize_continues
        ):
            self.state.finalize_continues += 1
            return "localize", "You chose to continue: " + str(parsed.get("reason") or "")
        return "done", None

    # -- runtime actions ----------------------------------------------------------------
    def enter(self, phase: Phase, reason: str | None = None) -> None:
        self.state.enter(phase, self.tracker.steps, reason)
        self.trace.add(
            "phase",
            step=self.tracker.steps,
            phase=str(phase),
            reason=reason,
            state=self.state.to_record(),
        )

    def model_call(self, tools: Sequence[ToolSpec]) -> ModelResponse | Termination:
        """One model step.  ``tools`` is the phase's set; an empty set means a
        decision point: the tool definitions are still sent (the history holds
        tool calls, which both providers accept only when tools are defined) but
        with ``tool_choice="none"``, so the model can only answer in text."""
        limit = self.tracker.exceeded()
        if limit:
            return BUDGET_TERMINATION[limit]
        self.tracker.steps += 1
        self.state.count_step()
        choice = "auto" if tools else "none"
        offered = list(tools) if tools else [self.specs[name] for name in MODEL_TOOLS]
        try:
            response = self.client.complete(
                self.messages,
                tools=offered,
                max_tokens=self.agent.max_output_tokens,
                tool_choice=choice,
            )
        except ModelError as exc:
            self.trace.add("model_call", step=self.tracker.steps, error=str(exc))
            self.final_text = str(exc)
            return Termination.MODEL_ERROR
        self.tracker.ledger.record(response)
        self.trace.add(
            "model_call",
            step=self.tracker.steps,
            phase=str(self.state.phase),
            **response.to_record(),
            text=clip(response.text),
            calls=[
                {"id": c.id, "name": c.name, "arguments": clip_arguments(c.arguments)}
                for c in response.tool_calls
            ],
        )
        self.messages.append(response.as_message())
        if response.text:
            self.final_text = response.text
        if not response.tool_calls:
            return self.spent() or response
        # With tool calls pending, the caller executes them first and checks the
        # budget after: the tokens are already paid for, the calls are free, and
        # an edit in that last reply must reach the workspace (issue #7).
        return response

    def spent(self) -> Termination | None:
        """The token or cost limit crossed by the model calls so far, as a termination."""
        limit = self.tracker.exceeded()
        if limit in ("tokens", "cost"):
            return BUDGET_TERMINATION[limit]
        return None

    def execute_calls(
        self,
        calls: Sequence[ToolCall],
        allowed: Sequence[str],
        edit_cap: int | None = None,
    ) -> tuple[Termination | None, int]:
        """Run one turn's tool calls under the phase policy.

        Returns the termination the calls forced (loop) or None, and the number
        of successful edits (``edit_cap`` refuses edits beyond it).  Every call the
        model asks for counts as a tool call, executed or refused; the loop
        detector sees all of them, so a refused call repeated three times is a
        loop like any other.
        """
        termination: Termination | None = None
        edits = 0
        for index, call in enumerate(calls):
            started = time.perf_counter()
            result, kind = self.policy(
                call, allowed, edit_cap - edits if edit_cap is not None else None
            )
            if kind == "loop_terminated":
                termination = Termination.AGENT_LOOP
            elif kind == "executed" and not result.is_error:
                path = call.arguments.get("path", "")
                if call.name == "read_file":
                    self.files_read.append(path)
                    self.state.visit(path)
                elif call.name == "edit_file":
                    self.files_edited.append(path)
                    self.state.visit(path)
                    edits += 1
            self.trace.add(
                "tool_call",
                step=self.tracker.steps,
                phase=str(self.state.phase),
                index=index,
                id=call.id,
                name=call.name,
                arguments=clip_arguments(call.arguments),
                parse_error=call.parse_error,
                duration_ms=int((time.perf_counter() - started) * 1000),
                is_error=result.is_error,
                invalid=bool(result.meta.get("invalid") or call.parse_error),
                policy=kind,
                meta={k: v for k, v in result.meta.items() if k != "invalid"},
                output=clip(result.output),
            )
            self.messages.append(tool_result(call.id, result.output, is_error=result.is_error))
            if termination is not None:
                for skipped in calls[index + 1 :]:
                    self.messages.append(
                        tool_result(skipped.id, "the run is ending; not executed", is_error=True)
                    )
                break
        self.state.repeated_tool_calls = self.loops.repeats()
        return termination, edits

    def policy(
        self, call: ToolCall, allowed: Sequence[str], edits_left: int | None
    ) -> tuple[ToolResult, str]:
        """Decide one call: the result the model sees and the policy that produced it."""
        if not self.tracker.can_call_tool():
            return (
                ToolResult.error(
                    f"tool-call budget exhausted ({self.budget.max_tool_calls}); "
                    "end the turn as instructed for this phase"
                ),
                "budget",
            )
        self.tracker.tool_calls += 1
        if call.parse_error:
            self.invalid_tool_calls += 1
            return (
                ToolResult.error(
                    f"{call.name}: {call.parse_error}; resend the call with valid JSON arguments",
                    invalid=True,
                ),
                "invalid",
            )
        verdict = self.loops.check(call.name, call.arguments)
        if verdict == "terminate":
            return (
                ToolResult.error("identical call repeated after the notice; the run ends here"),
                "loop_terminated",
            )
        if verdict == "notice":
            self.state.loop_interventions += 1
            return ToolResult.error(loop_notice(call.name)), "loop_notice"
        if call.name not in allowed:
            if call.name not in self.specs:
                self.invalid_tool_calls += 1
                return (
                    ToolResult.error(
                        f"unknown tool {call.name!r}; available: {', '.join(allowed)}",
                        invalid=True,
                    ),
                    "invalid",
                )
            self.state.refused_tool_calls += 1
            return ToolResult.error(phase_refusal(self.state.phase, call.name)), "phase_refused"
        if call.name == "edit_file" and edits_left is not None and edits_left <= 0:
            self.state.refused_tool_calls += 1
            return (
                ToolResult.error(phase_limit_notice("PATCH", self.limits.patch_edits) + " (edits)"),
                "edit_limit",
            )
        result = self.toolbox.call(call.name, call.arguments)
        if result.meta.get("invalid"):
            self.invalid_tool_calls += 1
            return result, "invalid"
        return result, "executed"

    def run_tests(self, label: str) -> TestSummary | None:
        """Run the task's full test command; None when the test-run budget is spent."""
        if not self.tracker.can_run_tests():
            self.trace.add("test_run", step=self.tracker.steps, phase=label, refused=True)
            return None
        started = time.perf_counter()
        result = self.toolbox.run_tests()
        self.tracker.test_runs += 1
        counts = result.meta.get("counts") or {}
        failing = list(result.meta.get("failed") or [])
        initial = set(self.state.initial_failures)
        if label == "reproduce":
            fixed, still, new = [], failing, []
        else:
            fixed = sorted(initial - set(failing))
            still = sorted(initial & set(failing))
            new = sorted(set(failing) - initial)
        summary = TestSummary(
            step=self.tracker.steps,
            phase=label,
            passed=int(counts.get("passed", 0)),
            failed=int(counts.get("failed", 0)),
            errors=int(counts.get("error", 0)),
            fixed=fixed,
            still_failing=still,
            newly_failing=new,
            is_error=result.is_error,
            detail=result.output,
        )
        self.state.test_history.append(summary)
        self.last_test_output = _describe(summary, initial_count=len(initial))
        record = summary.to_record()
        record.pop("step")
        record.pop("phase")
        self.trace.add(
            "test_run",
            step=self.tracker.steps,
            phase=label,
            duration_ms=int((time.perf_counter() - started) * 1000),
            **record,
            fixed_tests=fixed[:20],
            still_failing_tests=still[:20],
            output=clip(result.output),
        )
        return summary

    def nudge(self, text: str) -> None:
        self.state.nudges += 1
        self.intervention("nudge", text=text)
        self.messages.append(user(text))

    def intervention(self, name: str, **data: Any) -> None:
        self.trace.add(
            "intervention", step=self.tracker.steps, phase=str(self.state.phase), name=name, **data
        )

    def finish(self, termination: Termination, detail: str | None = None) -> AgentRun:
        workspace = self.toolbox.workspace
        patch = workspace.diff()
        changed = workspace.changed_files()
        last = self.state.test_history[-1] if self.state.test_history else None
        verified = bool(last and last.phase == "verify" and last.green and patch.strip())
        first_green = self.state.first_green_step
        runtime = {
            **self.state.to_record(),
            "limits": self.limits.to_record(),
            "verified": verified,
            "steps_after_green": (self.tracker.steps - first_green) if first_green else 0,
            "transitions": self.state.transitions,
        }
        self.trace.add("state", step=self.tracker.steps, **self.state.to_record(full=True))
        self.trace.add("patch", changed_files=changed, bytes=len(patch.encode("utf-8")), diff=patch)
        self.trace.add(
            "run_end",
            termination=str(termination),
            detail=detail,
            budget=self.tracker.to_record(),
            invalid_tool_calls=self.invalid_tool_calls,
            test_runs_refused=0,
            ledger=self.tracker.ledger.to_record(),
            final_text=clip(self.final_text),
            verified=verified,
        )
        return AgentRun(
            termination=termination,
            detail=detail,
            steps=self.tracker.steps,
            tool_calls=self.tracker.tool_calls,
            invalid_tool_calls=self.invalid_tool_calls,
            test_runs=self.tracker.test_runs,
            test_runs_refused=0,
            elapsed_seconds=self.tracker.elapsed_seconds,
            ledger=self.tracker.ledger.to_record(),
            final_text=self.final_text,
            patch=patch,
            changed_files=changed,
            files_read=sorted(set(self.files_read)),
            files_edited=sorted(set(self.files_edited)),
            trace=self.trace,
            runtime=runtime,
        )


# ----------------------------------------------------------------------------- helpers


def _with_note(instructions: str, note: str | None) -> str:
    return f"{note.strip()}\n\n{instructions}" if note else instructions


def _describe(summary: TestSummary, *, initial_count: int) -> str:
    """The runtime's own reading of a test run, prepended to pytest's summary."""
    if summary.phase == "reproduce":
        return summary.detail
    lines = [
        f"Runtime summary: fixed {len(summary.fixed)} of {initial_count} initially failing "
        f"test(s); {len(summary.still_failing)} still failing; "
        f"{len(summary.newly_failing)} newly failing (introduced by your change)."
    ]
    if summary.newly_failing:
        lines.append("Newly failing: " + ", ".join(summary.newly_failing[:10]))
    return "\n".join(lines) + "\n\n" + summary.detail
