"""Bounded agent control flow without provider or framework dependencies."""

import time
from collections.abc import Callable, Sequence
from copy import deepcopy
from typing import Protocol
from uuid import uuid4

from app.agent.models import (
    AgentMessage,
    AgentProposal,
    AgentRunResult,
    MessageRole,
    RunStatus,
    StopReason,
    ToolData,
    ToolCall,
    ToolExecutionResult,
)
from app.observability import (
    Component,
    EventName,
    NULL_OBSERVER,
    ObservationContext,
    ObservationEvent,
    Observer,
    OutcomeCode,
    safe_emit,
)


MAX_STEPS = 8
MAX_CONSECUTIVE_TOOL_CALLS = 3


class LLMClient(Protocol):
    def complete(self, messages: Sequence[AgentMessage]) -> AgentProposal: ...


class ToolExecutor(Protocol):
    """Execute a call and return JSON-compatible, deepcopyable data."""

    def execute(
        self, proposal: AgentProposal, tool_call: ToolCall
    ) -> ToolExecutionResult: ...


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolExecutor,
        *,
        max_steps: int = MAX_STEPS,
        observer: Observer = NULL_OBSERVER,
        context: ObservationContext | None = None,
        clock: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        if (
            isinstance(max_steps, bool)
            or not isinstance(max_steps, int)
            or max_steps <= 0
        ):
            raise ValueError("max_steps must be positive integer")
        self.llm = llm
        self.tools = tools
        self.max_steps = min(max_steps, MAX_STEPS)
        self._observer = observer
        self._context = context or ObservationContext(trace_id=uuid4())
        self._clock = clock

    def run(self, initial_messages: Sequence[AgentMessage]) -> AgentRunResult:
        messages = deepcopy(list(initial_messages))
        steps = 0
        previous_action_key: str | None = None
        consecutive_tool_calls = 0

        while steps < self.max_steps:
            try:
                proposal = self.llm.complete(deepcopy(tuple(messages)))
            except Exception:
                self._emit_run(
                    status=RunStatus.FAILED,
                    reason=None,
                    steps=steps,
                )
                raise
            steps += 1
            tool_call = deepcopy(proposal.tool_call)
            messages.append(
                AgentMessage(
                    role=MessageRole.ASSISTANT,
                    content=proposal.rationale_short,
                    proposal=deepcopy(proposal),
                    tool_call=tool_call,
                )
            )

            if proposal.is_terminal:
                self._emit_step(
                    step=steps,
                    outcome=OutcomeCode.TERMINAL,
                    action_key=tool_call.name if tool_call is not None else None,
                )
                return self._finish_result(
                    AgentRunResult(
                    status=RunStatus.COMPLETED,
                    reason=StopReason.FINAL,
                    messages=tuple(deepcopy(messages)),
                    steps=steps,
                    final_response=proposal.rationale_short,
                    proposal=deepcopy(proposal),
                    )
                )

            if tool_call is None:
                self._emit_step(step=steps, outcome=OutcomeCode.INVALID_SCHEMA)
                return self._finish_result(
                    AgentRunResult(
                    status=RunStatus.FAILED,
                    reason=StopReason.INVALID_PROPOSAL,
                    messages=tuple(deepcopy(messages)),
                    steps=steps,
                    final_response=None,
                    )
                )

            if tool_call.name == previous_action_key:
                consecutive_tool_calls += 1
            else:
                previous_action_key = tool_call.name
                consecutive_tool_calls = 1

            if consecutive_tool_calls == MAX_CONSECUTIVE_TOOL_CALLS:
                self._emit_step(
                    step=steps,
                    outcome=OutcomeCode.FAILED,
                    action_key=tool_call.name,
                )
                return self._finish_result(
                    AgentRunResult(
                    status=RunStatus.FAILED,
                    reason=StopReason.REPEATED_TOOL,
                    messages=tuple(deepcopy(messages)),
                    steps=steps,
                    final_response=None,
                    )
                )

            self._emit_step(
                step=steps,
                outcome=OutcomeCode.TOOL_REQUESTED,
                action_key=tool_call.name,
            )
            execution = self.tools.execute(
                deepcopy(proposal), deepcopy(tool_call)
            )
            messages.append(
                AgentMessage(
                    role=MessageRole.TOOL,
                    tool_call=tool_call,
                    tool_result=deepcopy(execution.data),
                )
            )

            if not execution.continue_run:
                return self._finish_result(
                    AgentRunResult(
                    status=RunStatus.COMPLETED,
                    reason=StopReason.EXECUTOR_STOPPED,
                    messages=tuple(deepcopy(messages)),
                    steps=steps,
                    final_response=execution.final_response,
                    proposal=deepcopy(proposal),
                    )
                )

        return self._finish_result(
            AgentRunResult(
                status=RunStatus.FAILED,
                reason=StopReason.MAX_STEPS,
                messages=tuple(deepcopy(messages)),
                steps=steps,
                final_response=None,
            )
        )

    def _finish_result(self, result: AgentRunResult) -> AgentRunResult:
        self._emit_run(
            status=result.status,
            reason=result.reason,
            steps=result.steps,
        )
        return result

    def _emit_step(
        self,
        *,
        step: int,
        outcome: OutcomeCode,
        action_key: str | None = None,
    ) -> None:
        known_actions = self._known_action_keys()
        action_known = (
            None
            if action_key is None
            else action_key in known_actions
        )
        safe_emit(
            self._observer,
            ObservationEvent(
                event=EventName.AGENT_STEP_COMPLETED,
                trace_id=self._context.trace_id,
                request_id=self._context.request_id,
                tenant_id=self._context.tenant_id,
                job_id=self._context.job_id,
                component=Component.AGENT,
                scenario_key=self._context.scenario_key,
                profile_fingerprint=self._context.profile_fingerprint,
                outcome=outcome,
                step=step,
                action_key=(action_key if action_known else None),
                action_known=action_known,
            ),
        )

    def _known_action_keys(self) -> frozenset[str]:
        definitions = getattr(self.tools, "definitions", None)
        if isinstance(definitions, dict):
            return frozenset(definitions)
        try:
            return frozenset(definitions.keys())
        except AttributeError:
            return frozenset()

    def _emit_run(
        self,
        *,
        status: RunStatus,
        reason: StopReason | None,
        steps: int,
    ) -> None:
        safe_emit(
            self._observer,
            ObservationEvent(
                event=EventName.AGENT_RUN_FINISHED,
                trace_id=self._context.trace_id,
                request_id=self._context.request_id,
                tenant_id=self._context.tenant_id,
                job_id=self._context.job_id,
                component=Component.AGENT,
                scenario_key=self._context.scenario_key,
                profile_fingerprint=self._context.profile_fingerprint,
                outcome=(
                    OutcomeCode.SUCCEEDED
                    if status is RunStatus.COMPLETED
                    else OutcomeCode.FAILED
                ),
                steps=steps,
                stop_reason=reason,
            ),
        )
