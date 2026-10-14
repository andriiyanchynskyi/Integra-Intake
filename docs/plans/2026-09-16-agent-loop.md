# Implementation plan: Phase 4 controlled agent loop

> **For agents:** Use the `executing-plans` skill to implement this plan task by task. Steps use checkbox syntax (`- [ ]`) for tracking.

**Purpose:** Add a provider-independent agent loop that supports bounded tool-call execution and deterministic stop conditions.

**Architecture:** `AgentLoop` owns the transcript and control state. It calls a narrow `LLMClient` protocol for a proposal and a `ToolExecutor` protocol for data. The loop has no FastAPI, database, queue, provider, policy, or tenant dependency. Tests use fakes only.

**Key files/stack:** Python 3.12+, standard-library `dataclasses`, `enum`, and `typing.Protocol`; pytest; existing `pytest -q` configuration.

---

## File map

| File | Responsibility |
| --- | --- |
| `app/agent/__init__.py` | Export the public loop API. |
| `app/agent/models.py` | Immutable message, tool-call, proposal, status, reason, and run-result types plus JSON-compatible tool data. |
| `app/agent/loop.py` | Protocols and the bounded `while` control flow. |
| `tests/test_agent_loop.py` | Fake dependencies and loop-contract unit tests. |

Existing tenant, domain, API, persistence, and profile files stay unchanged.

## Task 1: Add typed agent models

**Files:**

- Create: `app/agent/models.py`
- Create: `app/agent/__init__.py`

- [ ] **Step 1: Create `app/agent/models.py` with the loop data contract**

```python
"""Typed data exchanged by the provider-independent agent loop."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum


type ToolData = (
    None
    | bool
    | int
    | float
    | str
    | list[ToolData]
    | dict[str, ToolData]
)


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class RunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"


class StopReason(str, Enum):
    FINAL = "final"
    INVALID_PROPOSAL = "invalid_proposal"
    REPEATED_TOOL = "repeated_tool"
    MAX_STEPS = "max_steps"


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: Mapping[str, ToolData] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentProposal:
    final_response: str | None = None
    tool_call: ToolCall | None = None


@dataclass(frozen=True, slots=True)
class AgentMessage:
    role: MessageRole
    content: str | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolData = None


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    status: RunStatus
    reason: StopReason
    messages: tuple[AgentMessage, ...]
    steps: int
    final_response: str | None
```

The production model also normalizes these recursive values into immutable
dict/list-shaped containers and rejects non-finite numbers. The abbreviated
alias above describes the accepted external data shape.

`AgentProposal` deliberately permits an empty proposal so the loop can report
`invalid_proposal`. A proposal containing a final response takes precedence
over its optional tool call, matching the loop contract.

- [ ] **Step 2: Create `app/agent/__init__.py` with explicit exports**

```python
"""Controlled, provider-independent agent-loop interfaces."""

from app.agent.loop import AgentLoop, LLMClient, MAX_STEPS, ToolExecutor
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

__all__ = [
    "AgentLoop",
    "AgentMessage",
    "AgentProposal",
    "AgentRunResult",
    "LLMClient",
    "MAX_STEPS",
    "MessageRole",
    "RunStatus",
    "StopReason",
    "ToolData",
    "ToolCall",
    "ToolExecutor",
]
```

- [ ] **Step 3: Check that the model module compiles before the loop module exists**

Run: `python -m compileall -q app/agent`

Expected: exit code `0`.

## Task 2: Implement the controlled loop and dependency protocols

**Files:**

- Create: `app/agent/loop.py`
- Edit: `app/agent/__init__.py`

- [ ] **Step 1: Create `app/agent/loop.py`**

```python
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
                    content=proposal.final_response,
                    tool_call=tool_call,
                )
            )

            if proposal.final_response is not None:
                return AgentRunResult(
                    status=RunStatus.COMPLETED,
                    reason=StopReason.FINAL,
                    messages=tuple(deepcopy(messages)),
                    steps=steps,
                    final_response=proposal.final_response,
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
```

`steps` counts LLM proposals. The third identical consecutive tool call counts
as a proposal but does not execute. A tool message retains `tool_result=None`
when a tool returns `None`; role `TOOL` identifies that value as a completed
tool call rather than a missing message.

- [ ] **Step 2: Export `MAX_CONSECUTIVE_TOOL_CALLS` from `app/agent/__init__.py`**

Replace the import and `__all__` entries with:

```python
from app.agent.loop import (
    AgentLoop,
    LLMClient,
    MAX_CONSECUTIVE_TOOL_CALLS,
    MAX_STEPS,
    ToolExecutor,
)

# Add "MAX_CONSECUTIVE_TOOL_CALLS" immediately before "MAX_STEPS" in __all__.
```

- [ ] **Step 3: Check the module syntax and public imports**

Run: `python -m compileall -q app/agent; python -c "from app.agent import AgentLoop, MAX_CONSECUTIVE_TOOL_CALLS, MAX_STEPS; print(MAX_CONSECUTIVE_TOOL_CALLS, MAX_STEPS)"`

Expected: exit code `0` and `3 8`.

## Task 3: Add unit coverage through `unit-test-writer`

**Files:**

- Create: `tests/test_agent_loop.py`

- [ ] **Step 1: Invoke the `unit-test-writer` skill through an Agent task**

Give the test-writing agent this context:

```text
Write tests only in tests/test_agent_loop.py for app.agent.models and app.agent.loop.
Do not alter app code. Use synchronous local FakeLLM and FakeToolExecutor classes.
The loop accepts initial messages and must satisfy these assertions:
1. [tool call find_customer, final "done"] returns completed/final, steps == 2,
   executes find_customer once, and preserves the transcript order.
2. A tool returning None creates a role TOOL message whose tool_result is None,
   then a following final proposal completes the run.
3. [same tool three times] returns failed/repeated_tool with steps == 3 and only
   two executor calls.
4. [tool_a, tool_b, tool_a, final] completes and invokes all three calls,
   proving only consecutive repetition stops the loop.
5. Eight unique non-final tool proposals return failed/max_steps with steps == 8.
6. An AgentProposal() returns failed/invalid_proposal with no executor calls.
7. `max_steps` values `0`, `-1`, `0.5`, and `True` raise `ValueError`.
8. Self-referential list and mapping data raise `TypeError` with
   `cannot contain cycles`.
```

- [ ] **Step 2: Run the focused agent-loop tests**

Run: `pytest tests/test_agent_loop.py -q`

Expected: all tests pass.

- [ ] **Step 3: Run the full regression suite**

Run: `pytest -q`

Expected: all tests pass; PostgreSQL-only tests may skip when `DATABASE_URL` is not configured.

## Task 4: Review and verify the completed implementation

**Files:**

- Review: `app/agent/__init__.py`
- Review: `app/agent/models.py`
- Review: `app/agent/loop.py`
- Review: `tests/test_agent_loop.py`

- [ ] **Step 1: Run repository checks**

Run: `git diff --check; pytest -q`

Expected: no whitespace errors and a passing test suite, apart from documented integration skips.

- [ ] **Step 2: Invoke `code-review` through an Agent task**

Provide the reviewer with the phase-4 specification, this plan, and the final
diff. Ask it to inspect loop termination, transcript immutability at the public
boundary, `None` preservation, test coverage, accidental phase-scope growth,
and tenant-isolation regressions. Do not modify code from review findings;
report findings to the user first.

- [ ] **Step 3: Invoke `verification-before-completion` before declaring the phase complete**

Run the skill after the focused and full tests, `git diff --check`, and review
are complete. Report the commands and their actual results. Do not claim the
phase is complete without that evidence.

## Plan self-review

- The typed model task covers messages, proposals, tool calls, statuses, and
  results from the specification.
- The loop task covers a `while` loop, the eight-step limit, final precedence,
  invalid proposals, repeated tools, and tool-result preservation.
- The test task covers each required outcome and the exact third-call boundary.
- The review task preserves the required unit-test, review, and verification
  gates without adding real provider, policy, database, or transport work.
- The plan modifies no tenant-scoped persistence, authentication, YAML profile,
  or API module.

## Review follow-up approved by the user

The implementation also snapshots mutable boundary data with `copy.deepcopy`
before handing it to or receiving it from injected dependencies. Model
boundaries recursively freeze finite, acyclic JSON-compatible mappings and
lists, and the tool-executor protocol documents that result contract. It
rejects non-positive or non-integer `max_steps`, keeps the hard ceiling at
eight, and adds regression tests proving that nested input/result mutation
cannot alter the loop transcript. The `None` result test checks the next LLM
request as well as the returned result.
