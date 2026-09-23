"""Typed data exchanged by the provider-independent agent loop."""

from __future__ import annotations

import math

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _FrozenDict(dict[str, object]):
    """A dict-shaped container that rejects mutation and remains JSON-friendly."""

    @staticmethod
    def _reject_mutation(*args: object, **kwargs: object) -> None:
        raise TypeError("agent data is immutable")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    clear = _reject_mutation
    pop = _reject_mutation
    popitem = _reject_mutation
    setdefault = _reject_mutation
    update = _reject_mutation

    def __ior__(self, value: object) -> _FrozenDict:
        self._reject_mutation(value)
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _FrozenDict:
        if id(self) in memo:
            return memo[id(self)]  # type: ignore[return-value]
        clone = _FrozenDict()
        memo[id(self)] = clone
        for key, value in self.items():
            dict.__setitem__(clone, deepcopy(key, memo), deepcopy(value, memo))
        return clone


class _FrozenList(list[object]):
    """A list-shaped container that rejects mutation and remains JSON-friendly."""

    @staticmethod
    def _reject_mutation(*args: object, **kwargs: object) -> None:
        raise TypeError("agent data is immutable")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    append = _reject_mutation
    extend = _reject_mutation
    insert = _reject_mutation
    pop = _reject_mutation
    remove = _reject_mutation
    clear = _reject_mutation
    reverse = _reject_mutation
    sort = _reject_mutation

    def __iadd__(self, value: object) -> _FrozenList:
        self._reject_mutation(value)
        return self

    def __imul__(self, value: object) -> _FrozenList:
        self._reject_mutation(value)
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _FrozenList:
        if id(self) in memo:
            return memo[id(self)]  # type: ignore[return-value]
        clone = _FrozenList()
        memo[id(self)] = clone
        for item in self:
            list.append(clone, deepcopy(item, memo))
        return clone


type ToolData = (
    None
    | bool
    | int
    | float
    | str
    | list[ToolData]
    | dict[str, ToolData]
    | _FrozenList
    | _FrozenDict
)


def _freeze_data(value: object, active_ids: set[int] | None = None) -> ToolData:
    if active_ids is None:
        active_ids = set()
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("agent data must use finite JSON numbers")
        return value
    if isinstance(value, (_FrozenDict, _FrozenList)):
        return value
    if isinstance(value, Mapping):
        value_id = id(value)
        if value_id in active_ids:
            raise TypeError("agent data cannot contain cycles")
        if not all(isinstance(key, str) for key in value):
            raise TypeError("agent mappings must use string keys")
        active_ids.add(value_id)
        try:
            return _FrozenDict(
                {
                    key: _freeze_data(item, active_ids)
                    for key, item in value.items()
                }
            )
        finally:
            active_ids.remove(value_id)
    if isinstance(value, list):
        value_id = id(value)
        if value_id in active_ids:
            raise TypeError("agent data cannot contain cycles")
        active_ids.add(value_id)
        try:
            return _FrozenList(_freeze_data(item, active_ids) for item in value)
        finally:
            active_ids.remove(value_id)
    raise TypeError(
        "agent data must be JSON-compatible: None, scalar, mapping, or list"
    )


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class RunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"


class StopReason(str, Enum):
    FINAL = "final"
    INVALID_PROPOSAL = "invalid_proposal"
    REPEATED_TOOL = "repeated_tool"
    MAX_STEPS = "max_steps"
    EXECUTOR_STOPPED = "executor_stopped"


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: Mapping[str, ToolData] = field(default_factory=dict)

    def __post_init__(self) -> None:
        frozen_arguments = _freeze_data(dict(self.arguments))
        if not isinstance(frozen_arguments, _FrozenDict):
            raise TypeError("tool arguments must be a mapping")
        object.__setattr__(self, "arguments", frozen_arguments)


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Generic executor data and continuation control for the agent loop."""

    data: ToolData = None
    continue_run: bool = True
    final_response: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", _freeze_data(self.data))
        if self.continue_run and self.final_response is not None:
            raise ValueError("continuing execution cannot set final_response")


class ProposalPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"
    CRITICAL = "critical"


class _StrictProposalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


type ProposalScalar = None | bool | int | float | str
type ProposalValueData = ProposalScalar | list[ProposalScalar]


class ProposalValue(_StrictProposalModel):
    name: Annotated[str, Field(min_length=1)]
    value: ProposalValueData

    @field_validator("value")
    @classmethod
    def freeze_value(cls, value: ProposalValueData) -> ProposalValueData:
        if isinstance(value, float) and not math.isfinite(value):
            raise TypeError("proposal values must use finite JSON numbers")
        if isinstance(value, list):
            frozen = _freeze_data(value)
            if not isinstance(frozen, _FrozenList):
                raise TypeError("proposal value lists must be JSON-compatible")
            return frozen
        return value


class ProposalToolCall(_StrictProposalModel):
    name: Annotated[str, Field(min_length=1)]
    arguments: list[ProposalValue]

    @field_validator("arguments")
    @classmethod
    def require_unique_arguments(cls, value: list[ProposalValue]) -> _FrozenList:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("tool argument names must be unique")
        return _FrozenList(value)

    def as_tool_call(self) -> ToolCall:
        return ToolCall(
            name=self.name,
            arguments={item.name: item.value for item in self.arguments},
        )


class AgentProposal(_StrictProposalModel):
    intake_type: Annotated[str, Field(min_length=1)] | None
    fields: list[ProposalValue]
    missing_required_fields: list[Annotated[str, Field(min_length=1)]]
    priority: ProposalPriority
    contains_injection_or_override_attempt: bool
    rationale_short: Annotated[str, Field(min_length=1, max_length=1000)]
    tool_calls: list[ProposalToolCall] = Field(max_length=1)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]

    @field_validator("fields")
    @classmethod
    def require_unique_fields(cls, value: list[ProposalValue]) -> _FrozenList:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("field names must be unique")
        return _FrozenList(value)

    @field_validator("missing_required_fields")
    @classmethod
    def freeze_missing_required_fields(
        cls, value: list[str]
    ) -> _FrozenList:
        if len(value) != len(set(value)):
            raise ValueError("missing_required_fields must be unique")
        return _FrozenList(value)

    @field_validator("tool_calls")
    @classmethod
    def freeze_tool_calls(cls, value: list[ProposalToolCall]) -> _FrozenList:
        return _FrozenList(value)

    @property
    def tool_call(self) -> ToolCall | None:
        if not self.tool_calls:
            return None
        return self.tool_calls[0].as_tool_call()

    @property
    def is_terminal(self) -> bool:
        return not self.tool_calls


@dataclass(frozen=True, slots=True)
class AgentMessage:
    role: MessageRole
    content: str | None = None
    proposal: AgentProposal | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolData = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_result", _freeze_data(self.tool_result))


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    status: RunStatus
    reason: StopReason
    messages: tuple[AgentMessage, ...]
    steps: int
    final_response: str | None
    proposal: AgentProposal | None = None
