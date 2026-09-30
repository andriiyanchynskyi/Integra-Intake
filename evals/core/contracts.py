"""Strict, source-free projections shared by deterministic scenario suites."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import re
from collections.abc import Sequence

from app.agent import AgentProposal
from app.policy import RiskSignals, TrustedSource
from app.tenants.config import RoutingStatus
from app.tenants.identifiers import SAFE_IDENTIFIER_PATTERN, SafeIdentifier


SAFE_RESULT_KEYS = frozenset(
    {
        "case_id",
        "scenario_key",
        "passed",
        "routing_status",
        "policy_reason",
        "action_key",
        "mismatch_codes",
    }
)

SAFE_MISMATCH_CODES = frozenset(
    {
        "action_key_mismatch",
        "approval_mismatch",
        "case_created_mismatch",
        "execution_failed",
        "llm_calls_mismatch",
        "missing_fields_mismatch",
        "policy_reason_mismatch",
        "routing_status_mismatch",
        "scripted_proposals_exhausted",
        "scripted_proposals_unused",
        "tenant_isolation_mismatch",
        "tool_failure_mismatch",
    }
)

_CASE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ScenarioCaseKind(str, Enum):
    SUCCESS = "success"
    MISSING_FIELDS = "missing_fields"
    UNKNOWN_INTAKE = "unknown_intake"
    APPROVAL = "approval"
    TOOL_FAILURE = "tool_failure"
    TENANT_ISOLATION = "tenant_isolation"


@dataclass(frozen=True, slots=True)
class ScenarioExpectation:
    """Only the deterministic business facts compared by a scenario case."""

    routing_status: RoutingStatus | None = None
    policy_reason: str | None = None
    action_key: SafeIdentifier | None = None
    missing_required_fields: tuple[str, ...] = ()
    approval_required: bool | None = None
    case_created: bool | None = None
    tenant_isolated: bool | None = None
    llm_calls: int | None = None

    def __post_init__(self) -> None:
        if self.routing_status is not None and not isinstance(
            self.routing_status, RoutingStatus
        ):
            object.__setattr__(self, "routing_status", RoutingStatus(self.routing_status))
        if self.action_key is not None and re.fullmatch(
            SAFE_IDENTIFIER_PATTERN, self.action_key
        ) is None:
            raise ValueError("action_key must be a safe identifier or None")
        if len(self.missing_required_fields) != len(
            set(self.missing_required_fields)
        ):
            raise ValueError("missing_required_fields must be unique")
        if any(not isinstance(item, str) or not item for item in self.missing_required_fields):
            raise ValueError("missing_required_fields must contain non-empty names")
        object.__setattr__(
            self,
            "missing_required_fields",
            tuple(sorted(self.missing_required_fields)),
        )
        if self.llm_calls is not None and (
            type(self.llm_calls) is not int or self.llm_calls < 0
        ):
            raise ValueError("llm_calls must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class ScenarioCase:
    """An immutable typed case; source data is never part of its safe result."""

    case_id: str
    scenario_key: SafeIdentifier
    kind: ScenarioCaseKind
    source: TrustedSource
    proposals: tuple[AgentProposal, ...]
    expected: ScenarioExpectation
    risk_signals: RiskSignals = field(default_factory=RiskSignals)

    def __post_init__(self) -> None:
        if _CASE_ID_PATTERN.fullmatch(self.case_id) is None:
            raise ValueError("case_id must be lowercase kebab-case")
        if re.fullmatch(SAFE_IDENTIFIER_PATTERN, self.scenario_key) is None:
            raise ValueError("scenario_key must be a safe identifier")
        if not isinstance(self.kind, ScenarioCaseKind):
            object.__setattr__(self, "kind", ScenarioCaseKind(self.kind))
        proposals = tuple(AgentProposal.model_validate(item) for item in self.proposals)
        object.__setattr__(self, "proposals", proposals)
        if not isinstance(self.expected, ScenarioExpectation):
            raise TypeError("expected must be a ScenarioExpectation")
        if not isinstance(self.source, TrustedSource):
            raise TypeError("source must be a TrustedSource")


@dataclass(frozen=True, slots=True)
class ScenarioObservation:
    """Internal comparison data kept separate from the safe result projection."""

    routing_status: RoutingStatus | None = None
    policy_reason: str | None = None
    action_key: SafeIdentifier | None = None
    missing_required_fields: tuple[str, ...] = ()
    approval_required: bool = False
    case_created: bool = False
    tenant_isolated: bool | None = None
    llm_calls: int = 0


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """Closed result whose JSON projection has exactly seven allowlisted keys."""

    case_id: str
    scenario_key: SafeIdentifier
    passed: bool
    routing_status: RoutingStatus | None
    policy_reason: str | None
    action_key: SafeIdentifier | None
    mismatch_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _CASE_ID_PATTERN.fullmatch(self.case_id) is None:
            raise ValueError("case_id must be lowercase kebab-case")
        if re.fullmatch(SAFE_IDENTIFIER_PATTERN, self.scenario_key) is None:
            raise ValueError("scenario_key must be a safe identifier")
        if self.routing_status is not None and not isinstance(
            self.routing_status, RoutingStatus
        ):
            object.__setattr__(self, "routing_status", RoutingStatus(self.routing_status))
        if self.action_key is not None and re.fullmatch(
            SAFE_IDENTIFIER_PATTERN, self.action_key
        ) is None:
            raise ValueError("action_key must be a safe identifier or None")
        codes = tuple(sorted(set(self.mismatch_codes)))
        if any(code not in SAFE_MISMATCH_CODES for code in codes):
            raise ValueError("mismatch_codes contains an unknown code")
        object.__setattr__(self, "mismatch_codes", codes)

    @property
    def safe_projection(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "scenario_key": self.scenario_key,
            "passed": self.passed,
            "routing_status": (
                self.routing_status.value if self.routing_status is not None else None
            ),
            "policy_reason": self.policy_reason,
            "action_key": self.action_key,
            "mismatch_codes": list(self.mismatch_codes),
        }

    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        del mode
        return dict(self.safe_projection)

    def model_dump_json(self) -> str:
        return json.dumps(
            self.safe_projection,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def safe_failure_message(self) -> str:
        return ", ".join(self.mismatch_codes) or "scenario_case_failed"


def compare_scenario_observation(
    observation: ScenarioObservation,
    expected: ScenarioExpectation,
) -> tuple[str, ...]:
    """Compare only closed deterministic values and return allowlisted codes."""

    mismatches: list[str] = []
    comparisons: tuple[tuple[object, object, str], ...] = (
        (observation.routing_status, expected.routing_status, "routing_status_mismatch"),
        (observation.policy_reason, expected.policy_reason, "policy_reason_mismatch"),
        (observation.action_key, expected.action_key, "action_key_mismatch"),
        (
            observation.missing_required_fields,
            expected.missing_required_fields,
            "missing_fields_mismatch",
        ),
        (observation.approval_required, expected.approval_required, "approval_mismatch"),
        (observation.case_created, expected.case_created, "case_created_mismatch"),
        (
            observation.tenant_isolated,
            expected.tenant_isolated,
            "tenant_isolation_mismatch",
        ),
        (observation.llm_calls, expected.llm_calls, "llm_calls_mismatch"),
    )
    for actual, wanted, code in comparisons:
        if wanted is not None and actual != wanted:
            mismatches.append(code)
    return tuple(sorted(set(mismatches)))


def safe_result_from_observation(
    case: ScenarioCase,
    observation: ScenarioObservation,
    extra_mismatches: Sequence[str] = (),
) -> ScenarioResult:
    codes = list(compare_scenario_observation(observation, case.expected))
    codes.extend(
        code if code in SAFE_MISMATCH_CODES else "execution_failed"
        for code in extra_mismatches
    )
    return ScenarioResult(
        case_id=case.case_id,
        scenario_key=case.scenario_key,
        passed=not codes,
        routing_status=observation.routing_status,
        policy_reason=observation.policy_reason,
        action_key=observation.action_key,
        mismatch_codes=tuple(sorted(set(codes))),
    )


__all__ = [
    "SAFE_MISMATCH_CODES",
    "SAFE_RESULT_KEYS",
    "ScenarioCase",
    "ScenarioCaseKind",
    "ScenarioExpectation",
    "ScenarioObservation",
    "ScenarioResult",
    "compare_scenario_observation",
    "safe_result_from_observation",
]
