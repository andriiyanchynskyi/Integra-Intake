from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass

import pytest

from app.agent import (
    AgentLoop,
    AgentMessage,
    AgentProposal,
    MAX_STEPS,
    MessageRole,
    ProposalPriority,
    ProposalToolCall,
    RunStatus,
    StopReason,
    ToolCall,
    ToolData,
    ToolExecutionResult,
)


def _proposal(
    *,
    rationale: str = "Working on the request",
    tool_call: ToolCall | None = None,
    intake_type: str | None = "general",
    fields: Mapping[str, object] | None = None,
    source_excerpts: Mapping[str, str | None] | None = None,
    missing_required_fields: Iterable[str] = (),
    priority: ProposalPriority = ProposalPriority.NORMAL,
    contains_injection_or_override_attempt: bool = False,
    confidence: float = 0.9,
) -> AgentProposal:
    """Build a complete structured proposal for loop tests."""

    tool_calls = []
    if tool_call is not None:
        tool_calls.append(
            ProposalToolCall(
                name=tool_call.name,
                arguments=[
                    {"name": name, "value": value}
                    for name, value in tool_call.arguments.items()
                ],
            )
        )
    return AgentProposal(
        intake_type=intake_type,
        fields=[
            {
                "name": name,
                "value": value,
                **(
                    {"source_excerpt": source_excerpts[name]}
                    if source_excerpts is not None and name in source_excerpts
                    else {}
                ),
            }
            for name, value in (fields or {}).items()
        ],
        missing_required_fields=list(missing_required_fields),
        priority=priority,
        contains_injection_or_override_attempt=(
            contains_injection_or_override_attempt
        ),
        rationale_short=rationale,
        tool_calls=tool_calls,
        confidence=confidence,
    )


class FakeLLM:
    """Synchronous proposal source that records each transcript it receives."""

    def __init__(self, proposals: Iterable[AgentProposal]) -> None:
        self.proposals = list(proposals)
        self.requests: list[tuple[AgentMessage, ...]] = []

    def complete(self, messages: Sequence[AgentMessage]) -> AgentProposal:
        self.requests.append(deepcopy(tuple(messages)))
        if not self.proposals:
            raise AssertionError("the loop requested more proposals than expected")
        return self.proposals.pop(0)


class FakeToolExecutor:
    """Synchronous tool double that records calls and returns configured data."""

    def __init__(self, results: Mapping[str, ToolData] | None = None) -> None:
        self.results = dict(results or {})
        self.calls: list[ToolCall] = []

    def execute(
        self, proposal: AgentProposal, tool_call: ToolCall
    ) -> ToolExecutionResult:
        self.calls.append(tool_call)
        return ToolExecutionResult(data=self.results.get(tool_call.name))


def _mutate_nested_payload(value: object, errors: list[TypeError]) -> None:
    if isinstance(value, dict):
        try:
            value["mutated_by_llm"] = True
        except TypeError as error:
            errors.append(error)
        for nested in list(value.values()):
            _mutate_nested_payload(nested, errors)
    elif isinstance(value, list):
        try:
            value.append("mutated_by_llm")
        except TypeError as error:
            errors.append(error)


def _bypass_mutate_nested_payload(value: object) -> None:
    if isinstance(value, dict):
        dict.__setitem__(value, "mutated_by_llm", True)
        for nested in list(value.values()):
            _bypass_mutate_nested_payload(nested)
    elif isinstance(value, list):
        list.append(value, "mutated_by_llm")


class MutatingFakeLLM(FakeLLM):
    """LLM double that tries to alter every nested value in its input."""

    def __init__(self, proposals: Iterable[AgentProposal]) -> None:
        super().__init__(proposals)
        self.mutation_errors: list[TypeError] = []

    def complete(self, messages: Sequence[AgentMessage]) -> AgentProposal:
        proposal = super().complete(messages)
        for message in messages:
            _mutate_nested_payload(message.content, self.mutation_errors)
            _mutate_nested_payload(message.tool_result, self.mutation_errors)
            if message.tool_call is not None:
                _mutate_nested_payload(
                    message.tool_call.arguments, self.mutation_errors
                )
        return proposal


class CallerMutatingToolExecutor(FakeToolExecutor):
    """Tool double that mutates caller-owned payloads while executing."""

    def __init__(self, tool_arguments: dict[str, ToolData]) -> None:
        super().__init__({"snapshot_tool": "executed"})
        self.tool_arguments = tool_arguments

    def execute(
        self, proposal: AgentProposal, tool_call: ToolCall
    ) -> ToolExecutionResult:
        result = super().execute(proposal, tool_call)
        argument_nested = self.tool_arguments["query"]
        if isinstance(argument_nested, list):
            argument_nested.append("mutated_by_caller")
        return result


class StoppingFakeToolExecutor(FakeToolExecutor):
    """Tool double that returns a terminal execution outcome."""

    def __init__(self, result: ToolExecutionResult) -> None:
        super().__init__()
        self.result = result

    def execute(
        self, proposal: AgentProposal, tool_call: ToolCall
    ) -> ToolExecutionResult:
        self.calls.append(tool_call)
        return self.result


class BypassMutatingFakeLLM(FakeLLM):
    """LLM double that bypasses frozen-container overrides on its input."""

    def complete(self, messages: Sequence[AgentMessage]) -> AgentProposal:
        proposal = super().complete(messages)
        for message in messages:
            _bypass_mutate_nested_payload(message.tool_result)
            if message.tool_call is not None:
                _bypass_mutate_nested_payload(message.tool_call.arguments)
        return proposal


def test_tool_call_then_final_completes_with_ordered_transcript() -> None:
    initial = AgentMessage(role=MessageRole.USER, content="Find the customer")
    tool_call = ToolCall(name="find_customer", arguments={"email": "ada@example.com"})
    tool_proposal = _proposal(rationale="Searching for the customer", tool_call=tool_call)
    final_proposal = _proposal(rationale="Customer found")
    llm = FakeLLM(
        [
            tool_proposal,
            final_proposal,
        ]
    )
    tools = FakeToolExecutor({"find_customer": {"customer_id": "cust-1"}})

    result = AgentLoop(llm, tools).run([initial])

    assert result.status is RunStatus.COMPLETED
    assert result.reason is StopReason.FINAL
    assert result.steps == 2
    assert result.final_response == "Customer found"
    assert result.proposal == final_proposal
    assert tools.calls == [tool_call]
    assert result.messages == (
        initial,
        AgentMessage(
            role=MessageRole.ASSISTANT,
            content="Searching for the customer",
            proposal=tool_proposal,
            tool_call=tool_call,
        ),
        AgentMessage(
            role=MessageRole.TOOL,
            tool_call=tool_call,
            tool_result={"customer_id": "cust-1"},
        ),
        AgentMessage(
            role=MessageRole.ASSISTANT,
            content="Customer found",
            proposal=final_proposal,
        ),
    )
    assert llm.requests == [
        (initial,),
        (
            initial,
            AgentMessage(
                role=MessageRole.ASSISTANT,
                content="Searching for the customer",
                proposal=tool_proposal,
                tool_call=tool_call,
            ),
            AgentMessage(
                role=MessageRole.TOOL,
                tool_call=tool_call,
                tool_result={"customer_id": "cust-1"},
            ),
        ),
    ]


@pytest.mark.parametrize("confidence", [0.1, 0.99])
def test_confidence_does_not_change_completion_or_tool_authorization(
    confidence: float,
) -> None:
    tool_call = ToolCall(name="lookup_customer", arguments={"id": "cust-1"})
    tool_proposal = _proposal(
        rationale="Looking up the customer",
        tool_call=tool_call,
        confidence=confidence,
    )
    final_proposal = _proposal(
        rationale="Customer found",
        confidence=confidence,
    )
    tools = FakeToolExecutor({"lookup_customer": {"customer_id": "cust-1"}})
    llm = FakeLLM([tool_proposal, final_proposal])

    result = AgentLoop(llm, tools).run([])

    assert result.status is RunStatus.COMPLETED
    assert result.reason is StopReason.FINAL
    assert result.steps == 2
    assert result.final_response == "Customer found"
    assert result.proposal == final_proposal
    assert tools.calls == [tool_call]


def test_loop_preserves_optional_source_excerpt_in_structured_proposal() -> None:
    proposal = _proposal(
        rationale="Extracted a cited origin",
        fields={"origin": "Chicago"},
        source_excerpts={"origin": "Origin: Chicago"},
    )

    result = AgentLoop(FakeLLM([proposal]), FakeToolExecutor()).run([])

    assert result.status is RunStatus.COMPLETED
    assert result.reason is StopReason.FINAL
    assert result.proposal == proposal
    assert result.proposal is not None
    assert result.proposal.fields[0].source_excerpt == "Origin: Chicago"


def test_none_tool_result_is_preserved_before_final_response() -> None:
    tool_call = ToolCall(name="lookup_optional_note")
    tool_proposal = _proposal(rationale="Looking up the optional note", tool_call=tool_call)
    final_proposal = _proposal(rationale="No note exists")
    llm = FakeLLM(
        [
            tool_proposal,
            final_proposal,
        ]
    )
    tools = FakeToolExecutor({"lookup_optional_note": None})

    result = AgentLoop(llm, tools).run([])

    assert result.status is RunStatus.COMPLETED
    assert result.reason is StopReason.FINAL
    assert result.steps == 2
    assert tools.calls == [tool_call]
    tool_messages = [
        message for message in result.messages if message.role is MessageRole.TOOL
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call == tool_call
    assert tool_messages[0].tool_result is None
    next_request_tool_messages = [
        message for message in llm.requests[1] if message.role is MessageRole.TOOL
    ]
    assert len(next_request_tool_messages) == 1
    assert next_request_tool_messages[0].tool_call == tool_call
    assert next_request_tool_messages[0].tool_result is None


def test_executor_stopped_result_appends_data_once_and_preserves_response() -> None:
    tool_call = ToolCall(name="create_reply_draft")
    tool_proposal = _proposal(
        rationale="Drafting a reply",
        tool_call=tool_call,
    )
    tools = StoppingFakeToolExecutor(
        ToolExecutionResult(
            data={"draft_id": "draft-1"},
            continue_run=False,
            final_response="Reply draft is ready for review",
        )
    )

    result = AgentLoop(FakeLLM([tool_proposal]), tools).run([])

    assert result.status is RunStatus.COMPLETED
    assert result.reason is StopReason.EXECUTOR_STOPPED
    assert result.steps == 1
    assert result.final_response == "Reply draft is ready for review"
    assert result.proposal == tool_proposal
    assert tools.calls == [tool_call]
    assert [message.role for message in result.messages] == [
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
    ]
    tool_messages = [
        message for message in result.messages if message.role is MessageRole.TOOL
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call == tool_call
    assert tool_messages[0].tool_result == {"draft_id": "draft-1"}


def test_loop_isolates_initial_messages_from_mutating_llm() -> None:
    initial_payload = {"nested": {"items": ["caller"]}}
    initial = AgentMessage(role=MessageRole.USER, tool_result=initial_payload)
    final_proposal = _proposal(rationale="Finished")
    llm = MutatingFakeLLM([final_proposal])

    result = AgentLoop(llm, FakeToolExecutor()).run([initial])

    expected_payload = {"nested": {"items": ["caller"]}}
    assert initial_payload == expected_payload
    assert llm.mutation_errors
    assert all(isinstance(error, TypeError) for error in llm.mutation_errors)
    assert result.messages == (
        AgentMessage(role=MessageRole.USER, tool_result=expected_payload),
        AgentMessage(
            role=MessageRole.ASSISTANT,
            content="Finished",
            proposal=final_proposal,
        ),
    )


def test_loop_snapshots_tool_call_arguments_from_tool_executor() -> None:
    tool_arguments = {"query": ["original"]}
    tool_call = ToolCall(name="snapshot_tool", arguments=tool_arguments)
    tool_proposal = _proposal(rationale="Running snapshot tool", tool_call=tool_call)
    final_proposal = _proposal(rationale="Finished")
    llm = FakeLLM(
        [
            tool_proposal,
            final_proposal,
        ]
    )
    tools = CallerMutatingToolExecutor(tool_arguments)

    result = AgentLoop(llm, tools).run([])

    expected_arguments = {"query": ["original"]}
    assert tool_arguments == {"query": ["original", "mutated_by_caller"]}
    assert result.messages[0].tool_call is not None
    assert result.messages[0].tool_call.arguments == expected_arguments
    assert result.messages[1].tool_call is not None
    assert result.messages[1].tool_call.arguments == expected_arguments
    assert tools.calls[0].arguments == expected_arguments


def test_nested_agent_data_is_immutable_and_json_serializable() -> None:
    expected_arguments = {
        "filters": {"regions": ["EU", "US"]},
        "limit": 5,
    }
    expected_result = {
        "matches": [{"customer_id": "cust-1", "active": True}],
        "next_cursor": None,
    }
    tool_call = ToolCall(name="search_customers", arguments=expected_arguments)
    tool_message = AgentMessage(
        role=MessageRole.TOOL,
        tool_call=tool_call,
        tool_result=expected_result,
    )

    assert tool_call.arguments == expected_arguments
    assert tool_message.tool_result == expected_result
    assert json.loads(json.dumps(tool_call.arguments)) == expected_arguments
    assert json.loads(json.dumps(tool_message.tool_result)) == expected_result

    with pytest.raises(TypeError, match="agent data is immutable"):
        tool_call.arguments["filters"]["regions"].append("CA")
    with pytest.raises(TypeError, match="agent data is immutable"):
        tool_message.tool_result["matches"][0]["active"] = False

    assert tool_call.arguments == expected_arguments
    assert tool_message.tool_result == expected_result


def test_loop_snapshots_tool_result_from_mutating_llm() -> None:
    tool_call = ToolCall(name="snapshot_tool")
    tool_result = {"nested": {"items": ["from_tool"]}}
    tool_proposal = _proposal(rationale="Running snapshot tool", tool_call=tool_call)
    final_proposal = _proposal(rationale="Finished")
    llm = MutatingFakeLLM(
        [
            tool_proposal,
            final_proposal,
        ]
    )
    tools = FakeToolExecutor({"snapshot_tool": tool_result})

    result = AgentLoop(llm, tools).run([])

    expected_result = {"nested": {"items": ["from_tool"]}}
    assert tool_result == expected_result
    assert llm.mutation_errors
    assert all(isinstance(error, TypeError) for error in llm.mutation_errors)
    tool_messages = [
        message for message in result.messages if message.role is MessageRole.TOOL
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_result == expected_result
    next_request_tool_messages = [
        message for message in llm.requests[1] if message.role is MessageRole.TOOL
    ]
    assert len(next_request_tool_messages) == 1
    assert next_request_tool_messages[0].tool_result == expected_result


def test_llm_boundary_snapshot_survives_base_container_mutation() -> None:
    initial_payload = {"nested": {"items": ["caller"]}}
    tool_arguments = {"query": ["original"]}
    tool_result = {"nested": {"items": ["from_tool"]}}
    tool_call = ToolCall(name="snapshot_tool", arguments=tool_arguments)
    initial = AgentMessage(role=MessageRole.USER, tool_result=initial_payload)
    tool_proposal = _proposal(rationale="Running snapshot tool", tool_call=tool_call)
    final_proposal = _proposal(rationale="Finished")
    llm = BypassMutatingFakeLLM(
        [
            tool_proposal,
            final_proposal,
        ]
    )
    tools = FakeToolExecutor({"snapshot_tool": tool_result})

    result = AgentLoop(llm, tools).run([initial])

    assert initial_payload == {"nested": {"items": ["caller"]}}
    assert tool_arguments == {"query": ["original"]}
    assert tool_result == {"nested": {"items": ["from_tool"]}}
    assert result.messages[0].tool_result == {
        "nested": {"items": ["caller"]}
    }
    assert result.messages[1].tool_call is not None
    assert result.messages[1].tool_call.arguments == {"query": ["original"]}
    assert result.messages[2].tool_result == {
        "nested": {"items": ["from_tool"]}
    }
    assert llm.requests[1][0].tool_result == {
        "nested": {"items": ["caller"]}
    }
    assert llm.requests[1][1].tool_call is not None
    assert llm.requests[1][1].tool_call.arguments == {"query": ["original"]}
    assert llm.requests[1][2].tool_result == {
        "nested": {"items": ["from_tool"]}
    }


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_tool_data_is_rejected(value: float) -> None:
    with pytest.raises(TypeError, match="finite JSON"):
        ToolCall(name="invalid_number", arguments={"value": value})
    with pytest.raises(TypeError, match="finite JSON"):
        AgentMessage(role=MessageRole.TOOL, tool_result={"value": value})


@pytest.mark.parametrize("max_steps", [0, -1, 0.5, True])
def test_non_positive_max_steps_is_rejected(max_steps: int | float | bool) -> None:
    with pytest.raises(ValueError, match="max_steps must be positive integer"):
        AgentLoop(FakeLLM([]), FakeToolExecutor(), max_steps=max_steps)


@pytest.mark.parametrize("payload_kind", ["dict", "list"])
def test_self_referential_tool_data_is_rejected(payload_kind: str) -> None:
    if payload_kind == "dict":
        cyclic_dict: dict[str, object] = {}
        cyclic_dict["self"] = cyclic_dict
        payload: object = cyclic_dict
    else:
        cyclic_list: list[object] = []
        cyclic_list.append(cyclic_list)
        payload = cyclic_list

    with pytest.raises(TypeError, match="cannot contain cycles"):
        ToolCall(name="cyclic_payload", arguments={"payload": payload})
    with pytest.raises(TypeError, match="cannot contain cycles"):
        AgentMessage(role=MessageRole.TOOL, tool_result=payload)


def test_third_consecutive_request_for_same_tool_breaks_before_execution() -> None:
    tool_call = ToolCall(name="unstable_tool")
    llm = FakeLLM(
        [
            _proposal(rationale=f"Attempt {index}", tool_call=tool_call)
            for index in range(3)
        ]
    )
    tools = FakeToolExecutor({"unstable_tool": "ok"})

    result = AgentLoop(llm, tools).run([])

    assert result.status is RunStatus.FAILED
    assert result.reason is StopReason.REPEATED_TOOL
    assert result.steps == 3
    assert tools.calls == [tool_call, tool_call]
    assert [message.role for message in result.messages] == [
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]


def test_nonconsecutive_repeated_tool_requests_are_allowed() -> None:
    tool_a = ToolCall(name="tool_a")
    tool_b = ToolCall(name="tool_b")
    llm = FakeLLM(
        [
            _proposal(rationale="Running tool A", tool_call=tool_a),
            _proposal(rationale="Running tool B", tool_call=tool_b),
            _proposal(rationale="Running tool A again", tool_call=tool_a),
            _proposal(rationale="Finished"),
        ]
    )
    tools = FakeToolExecutor({"tool_a": "a", "tool_b": "b"})

    result = AgentLoop(llm, tools).run([])

    assert result.status is RunStatus.COMPLETED
    assert result.reason is StopReason.FINAL
    assert result.steps == 4
    assert tools.calls == [tool_a, tool_b, tool_a]


def test_eight_nonfinal_proposals_stop_at_max_steps() -> None:
    calls = [ToolCall(name=f"tool_{index}") for index in range(MAX_STEPS)]
    llm = FakeLLM(
        [_proposal(rationale=f"Running {call.name}", tool_call=call) for call in calls]
    )
    tools = FakeToolExecutor()

    result = AgentLoop(llm, tools).run([])

    assert result.status is RunStatus.FAILED
    assert result.reason is StopReason.MAX_STEPS
    assert result.steps == MAX_STEPS
    assert tools.calls == calls


def test_max_steps_argument_cannot_raise_the_hard_ceiling() -> None:
    calls = [ToolCall(name=f"tool_{index}") for index in range(MAX_STEPS + 1)]
    llm = FakeLLM(
        [_proposal(rationale=f"Running {call.name}", tool_call=call) for call in calls]
    )
    tools = FakeToolExecutor()

    result = AgentLoop(llm, tools, max_steps=MAX_STEPS + 1).run([])

    assert result.status is RunStatus.FAILED
    assert result.reason is StopReason.MAX_STEPS
    assert result.steps == MAX_STEPS
    assert tools.calls == calls[:MAX_STEPS]


@dataclass(frozen=True)
class InvalidProposal:
    """Malformed provider double used to exercise the loop's defensive branch."""

    is_terminal = False
    tool_call = None
    rationale_short = "Invalid structured proposal"


def test_empty_proposal_fails_without_executing_a_tool() -> None:
    llm = FakeLLM([InvalidProposal()])
    tools = FakeToolExecutor()

    result = AgentLoop(llm, tools).run([])

    assert result.status is RunStatus.FAILED
    assert result.reason is StopReason.INVALID_PROPOSAL
    assert result.steps == 1
    assert result.final_response is None
    assert tools.calls == []
    assert result.messages == (
        AgentMessage(
            role=MessageRole.ASSISTANT,
            content="Invalid structured proposal",
            proposal=InvalidProposal(),
        ),
    )
