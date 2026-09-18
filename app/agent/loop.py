"""Bounded agent control flow without provider or framework dependencies."""

from collections.abc import Sequence
from copy import deepcopy
from typing import Protocol

from app.agent.models import (
    AgentMessage,
    AgentProposal,
    AgentRunResult,
    MessageRole,
    RunStatus,
    StopReason,
    ToolData,
    ToolCall,
)


MAX_STEPS = 8
MAX_CONSECUTIVE_TOOL_CALLS = 3


class LLMClient(Protocol):
    def complete(self, messages: Sequence[AgentMessage]) -> AgentProposal: ...


class ToolExecutor(Protocol):
    """Execute a call and return JSON-compatible, deepcopyable data."""

    def execute(self, tool_call: ToolCall) -> ToolData: ...


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolExecutor,
        *,
        max_steps: int = MAX_STEPS,
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

    def run(self, initial_messages: Sequence[AgentMessage]) -> AgentRunResult:
        messages = deepcopy(list(initial_messages))
        steps = 0
        previous_tool_name: str | None = None
        consecutive_tool_calls = 0

        while steps < self.max_steps:
            proposal = self.llm.complete(deepcopy(tuple(messages)))
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
                return AgentRunResult(
                    status=RunStatus.COMPLETED,
                    reason=StopReason.FINAL,
                    messages=tuple(deepcopy(messages)),
                    steps=steps,
                    final_response=proposal.rationale_short,
                    proposal=deepcopy(proposal),
                )

            if tool_call is None:
                return AgentRunResult(
                    status=RunStatus.FAILED,
                    reason=StopReason.INVALID_PROPOSAL,
                    messages=tuple(deepcopy(messages)),
                    steps=steps,
                    final_response=None,
                )

            if tool_call.name == previous_tool_name:
                consecutive_tool_calls += 1
            else:
                previous_tool_name = tool_call.name
                consecutive_tool_calls = 1

            if consecutive_tool_calls == MAX_CONSECUTIVE_TOOL_CALLS:
                return AgentRunResult(
                    status=RunStatus.FAILED,
                    reason=StopReason.REPEATED_TOOL,
                    messages=tuple(deepcopy(messages)),
                    steps=steps,
                    final_response=None,
                )

            result = self.tools.execute(deepcopy(tool_call))
            messages.append(
                AgentMessage(
                    role=MessageRole.TOOL,
                    tool_call=tool_call,
                    tool_result=deepcopy(result),
                )
            )

        return AgentRunResult(
            status=RunStatus.FAILED,
            reason=StopReason.MAX_STEPS,
            messages=tuple(deepcopy(messages)),
            steps=steps,
            final_response=None,
        )
