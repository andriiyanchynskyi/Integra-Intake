"""Controlled, provider-independent agent-loop interfaces.

The public package is intentionally lazy.  Observation contracts refer to the
closed ``StopReason`` enum while the loop itself emits observations; eager
re-export imports would therefore create a package-initialization cycle.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agent.loop import (
        AgentLoop,
        LLMClient,
        ToolExecutor,
    )
    from app.agent.models import (
        AgentMessage,
        AgentProposal,
        AgentRunResult,
        MessageRole,
        RunStatus,
        StopReason,
        ToolExecutionDisposition,
        ProposalPriority,
        ProposalToolCall,
        ProposalValue,
        ToolData,
        ToolCall,
        ToolExecutionResult,
    )


_MODEL_EXPORTS = {
    "AgentMessage",
    "AgentProposal",
    "AgentRunResult",
    "MessageRole",
    "RunStatus",
    "StopReason",
    "ToolExecutionDisposition",
    "ProposalPriority",
    "ProposalToolCall",
    "ProposalValue",
    "ToolData",
    "ToolCall",
    "ToolExecutionResult",
}
_LOOP_EXPORTS = {
    "AgentLoop",
    "LLMClient",
    "MAX_CONSECUTIVE_TOOL_CALLS",
    "MAX_STEPS",
    "ToolExecutor",
}


def __getattr__(name: str) -> object:
    if name in _MODEL_EXPORTS:
        from app.agent import models

        value = getattr(models, name)
    elif name in _LOOP_EXPORTS:
        from app.agent import loop

        value = getattr(loop, name)
    else:
        raise AttributeError(name)
    globals()[name] = value
    return value

__all__ = [
    "AgentLoop",
    "AgentMessage",
    "AgentProposal",
    "AgentRunResult",
    "LLMClient",
    "MAX_CONSECUTIVE_TOOL_CALLS",
    "MAX_STEPS",
    "MessageRole",
    "ProposalPriority",
    "ProposalToolCall",
    "ProposalValue",
    "RunStatus",
    "StopReason",
    "ToolExecutionDisposition",
    "ToolData",
    "ToolCall",
    "ToolExecutionResult",
    "ToolExecutor",
]
