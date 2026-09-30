"""Synthetic repair cases with no provider, parser, or external data."""

from __future__ import annotations

from typing import Mapping

from app.agent import AgentProposal, ProposalPriority
from app.policy import RiskSignals, TrustedSource
from app.tenants.config import RoutingStatus

from evals.core import (
    ScenarioCase,
    ScenarioCaseKind,
    ScenarioExpectation,
)


def _proposal(
    *,
    intake_type: str,
    fields: Mapping[str, object],
    tool: str | None = "create_case",
    priority: ProposalPriority = ProposalPriority.NORMAL,
    rationale: str = "Synthetic repair proposal.",
) -> AgentProposal:
    return AgentProposal.model_validate(
        {
            "intake_type": intake_type,
            "fields": [
                {"name": name, "value": value} for name, value in fields.items()
            ],
            "missing_required_fields": [],
            "priority": priority,
            "contains_injection_or_override_attempt": False,
            "rationale_short": rationale,
            "tool_calls": (
                [{"name": tool, "arguments": []}] if tool is not None else []
            ),
            "confidence": 0.9,
        }
    )


def _source(case_name: str) -> TrustedSource:
    return TrustedSource(
        channel="synthetic",
        subject=f"Repair {case_name}",
        body=f"Synthetic repair source for {case_name}.",
    )


_COMPLETE = {
    "customer_name": "Synthetic customer",
    "contact": "customer@example.test",
    "issue": "Synthetic issue",
    "asset_type": "appliance",
}


REPAIR_CASES = (
    ScenarioCase(
        case_id="repair-success",
        scenario_key="repair_service",
        kind=ScenarioCaseKind.SUCCESS,
        source=_source("success"),
        proposals=(
            _proposal(intake_type="service_request", fields=_COMPLETE),
            _proposal(intake_type="service_request", fields={}, tool=None),
        ),
        expected=ScenarioExpectation(
            routing_status=RoutingStatus.READY,
            policy_reason="action_allowed",
            action_key="create_case",
            case_created=True,
            llm_calls=2,
        ),
    ),
    ScenarioCase(
        case_id="repair-missing-fields",
        scenario_key="repair_service",
        kind=ScenarioCaseKind.MISSING_FIELDS,
        source=_source("missing-fields"),
        proposals=(
            _proposal(
                intake_type="service_request",
                fields={key: value for key, value in _COMPLETE.items() if key != "issue"},
            ),
        ),
        expected=ScenarioExpectation(
            routing_status=RoutingStatus.AWAITING_INPUT,
            policy_reason="missing_required_fields",
            action_key="create_case",
            missing_required_fields=("issue",),
            case_created=False,
            llm_calls=1,
        ),
    ),
    ScenarioCase(
        case_id="repair-unknown-intake",
        scenario_key="repair_service",
        kind=ScenarioCaseKind.UNKNOWN_INTAKE,
        source=_source("unknown-intake"),
        proposals=(
            _proposal(intake_type="unknown_request", fields=_COMPLETE),
        ),
        expected=ScenarioExpectation(
            routing_status=RoutingStatus.REJECTED,
            policy_reason="unknown_intake_type",
            action_key="create_case",
            case_created=False,
            llm_calls=1,
        ),
    ),
    ScenarioCase(
        case_id="repair-approval",
        scenario_key="repair_service",
        kind=ScenarioCaseKind.APPROVAL,
        source=_source("approval"),
        proposals=(
            _proposal(intake_type="service_request", fields=_COMPLETE),
        ),
        expected=ScenarioExpectation(
            routing_status=RoutingStatus.URGENT,
            policy_reason="safety_or_legal_risk",
            action_key="create_case",
            approval_required=True,
            case_created=False,
            llm_calls=1,
        ),
        risk_signals=RiskSignals(safety_or_legal_risk=True),
    ),
    ScenarioCase(
        case_id="repair-tool-failure",
        scenario_key="repair_service",
        kind=ScenarioCaseKind.TOOL_FAILURE,
        source=_source("tool-failure"),
        proposals=(
            _proposal(intake_type="service_request", fields=_COMPLETE),
        ),
        expected=ScenarioExpectation(
            routing_status=RoutingStatus.READY,
            policy_reason="action_allowed",
            action_key="create_case",
            case_created=False,
            llm_calls=1,
        ),
    ),
    ScenarioCase(
        case_id="repair-tenant-isolation",
        scenario_key="repair_service",
        kind=ScenarioCaseKind.TENANT_ISOLATION,
        source=_source("tenant-isolation"),
        proposals=(),
        expected=ScenarioExpectation(tenant_isolated=True),
    ),
)

CASES = REPAIR_CASES


def scenario_cases() -> tuple[ScenarioCase, ...]:
    return REPAIR_CASES


__all__ = ["CASES", "REPAIR_CASES", "scenario_cases"]
