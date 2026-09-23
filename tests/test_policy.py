from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest

from app.agent.models import AgentProposal, ProposalPriority
from app.policy import (
    PolicyEngine,
    PolicyInput,
    RiskSignals,
    TrustedSource,
    TrustedToolRuntimeContext,
)
from app.tenants.config import RoutingDecision, RoutingStatus, TenantConfig


@pytest.fixture
def profile_data() -> dict[str, object]:
    return {
        "slug": "acme",
        "display_name": "Acme",
        "intake_types": [
            {
                "name": "request",
                "description": "A customer request",
                "required_fields": ["summary"],
            }
        ],
        "fields": {
            "summary": {"type": "short_text", "label": "Summary"},
        },
        "action_policy": {
            "create_case": {"allowed": True, "requires_approval": False},
            "find_customer": {"allowed": True, "requires_approval": False},
            "approval_action": {"allowed": True, "requires_approval": True},
            "send_reply": {"allowed": True, "requires_approval": False},
        },
        "routing": {
            "outcome_names": [
                "urgent",
                "awaiting_input",
                "pending_approval",
                "ready",
                "rejected",
            ],
            "always_approval_actions": ["send_reply"],
        },
    }


@pytest.fixture
def tenant_config(profile_data: dict[str, object]) -> TenantConfig:
    return TenantConfig.model_validate(profile_data)


def make_proposal(
    *,
    intake_type: str | None = "request",
    field_names: tuple[str, ...] = ("summary",),
    field_values: tuple[tuple[str, object], ...] | None = None,
    missing_required_fields: list[str] | None = None,
    injection: bool = False,
    priority: ProposalPriority = ProposalPriority.NORMAL,
    confidence: float = 0.5,
) -> AgentProposal:
    proposal_fields = (
        field_values
        if field_values is not None
        else tuple((name, f"value for {name}") for name in field_names)
    )
    return AgentProposal.model_validate(
        {
            "intake_type": intake_type,
            "fields": [
                {"name": name, "value": value}
                for name, value in proposal_fields
            ],
            "missing_required_fields": (
                []
                if missing_required_fields is None
                else missing_required_fields
            ),
            "priority": priority,
            "contains_injection_or_override_attempt": injection,
            "rationale_short": "Structured proposal for policy evaluation.",
            "tool_calls": [],
            "confidence": confidence,
        }
    )


def make_runtime(
    tenant_config: TenantConfig,
    *,
    safety_or_legal_risk: bool = False,
) -> TrustedToolRuntimeContext:
    return TrustedToolRuntimeContext(
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        tenant_config=tenant_config,
        source=TrustedSource(
            channel="email",
            subject="Customer request",
            body="Please help with this request.",
        ),
        risk_signals=RiskSignals(safety_or_legal_risk=safety_or_legal_risk),
    )


def evaluate(
    tenant_config: TenantConfig,
    proposal: AgentProposal,
    *,
    action: str,
    requires_complete_fields: bool = True,
    registered_actions: frozenset[str] | None = None,
    safety_or_legal_risk: bool = False,
):
    return PolicyEngine().evaluate(
        PolicyInput(
            proposal=proposal,
            requested_action=action,
            requires_complete_fields=requires_complete_fields,
            registered_actions=(
                frozenset(
                    {
                        "create_case",
                        "find_customer",
                        "approval_action",
                    }
                )
                if registered_actions is None
                else registered_actions
            ),
            runtime=make_runtime(
                tenant_config,
                safety_or_legal_risk=safety_or_legal_risk,
            ),
        )
    )


def assert_outcome(
    outcome,
    *,
    status: RoutingStatus,
    decision: RoutingDecision,
    reason: str,
    missing: tuple[str, ...] = (),
) -> None:
    assert outcome.status is status
    assert outcome.decision is decision
    assert outcome.reason == reason
    assert outcome.missing_required_fields == missing


def test_trusted_risk_precedes_unknown_intake_type_and_action(
    tenant_config: TenantConfig,
) -> None:
    outcome = evaluate(
        tenant_config,
        make_proposal(intake_type="not-configured", field_names=()),
        action="not-registered",
        registered_actions=frozenset(),
        safety_or_legal_risk=True,
    )

    assert_outcome(
        outcome,
        status=RoutingStatus.URGENT,
        decision=RoutingDecision.NEEDS_APPROVAL,
        reason="safety_or_legal_risk",
    )


def test_model_injection_signal_requests_review(
    tenant_config: TenantConfig,
) -> None:
    outcome = evaluate(
        tenant_config,
        make_proposal(injection=True),
        action="create_case",
    )

    assert_outcome(
        outcome,
        status=RoutingStatus.URGENT,
        decision=RoutingDecision.NEEDS_APPROVAL,
        reason="safety_or_legal_risk",
    )


def test_false_model_injection_signal_cannot_clear_trusted_risk(
    tenant_config: TenantConfig,
) -> None:
    outcome = evaluate(
        tenant_config,
        make_proposal(injection=False),
        action="create_case",
        safety_or_legal_risk=True,
    )

    assert_outcome(
        outcome,
        status=RoutingStatus.URGENT,
        decision=RoutingDecision.NEEDS_APPROVAL,
        reason="safety_or_legal_risk",
    )


def test_unknown_intake_type_precedes_missing_fields_and_unknown_action(
    tenant_config: TenantConfig,
) -> None:
    outcome = evaluate(
        tenant_config,
        make_proposal(intake_type="not-configured", field_names=()),
        action="not-registered",
        registered_actions=frozenset(),
    )

    assert_outcome(
        outcome,
        status=RoutingStatus.REJECTED,
        decision=RoutingDecision.DENY,
        reason="unknown_intake_type",
    )


def test_missing_required_summary_blocks_create_case(
    tenant_config: TenantConfig,
) -> None:
    outcome = evaluate(
        tenant_config,
        make_proposal(field_names=()),
        action="create_case",
    )

    assert_outcome(
        outcome,
        status=RoutingStatus.AWAITING_INPUT,
        decision=RoutingDecision.DENY,
        reason="missing_required_fields",
        missing=("summary",),
    )


def test_find_customer_can_run_without_complete_required_fields(
    tenant_config: TenantConfig,
) -> None:
    outcome = evaluate(
        tenant_config,
        make_proposal(field_names=()),
        action="find_customer",
        requires_complete_fields=False,
    )

    assert_outcome(
        outcome,
        status=RoutingStatus.READY,
        decision=RoutingDecision.ALLOW,
        reason="action_allowed",
    )


def test_send_reply_is_unknown_when_not_registered_even_if_yaml_configures_it(
    tenant_config: TenantConfig,
) -> None:
    outcome = evaluate(
        tenant_config,
        make_proposal(),
        action="send_reply",
        registered_actions=frozenset(
            {"create_case", "find_customer", "approval_action"}
        ),
    )

    assert_outcome(
        outcome,
        status=RoutingStatus.REJECTED,
        decision=RoutingDecision.DENY,
        reason="action_not_configured",
    )


def test_yaml_disallowed_action_is_denied(
    profile_data: dict[str, object],
) -> None:
    disallowed_profile = deepcopy(profile_data)
    disallowed_profile["action_policy"]["create_case"]["allowed"] = False  # type: ignore[index]
    tenant_config = TenantConfig.model_validate(disallowed_profile)

    outcome = evaluate(
        tenant_config,
        make_proposal(),
        action="create_case",
    )

    assert_outcome(
        outcome,
        status=RoutingStatus.REJECTED,
        decision=RoutingDecision.DENY,
        reason="action_not_allowed",
    )


def test_requires_approval_action_is_pending_approval(
    tenant_config: TenantConfig,
) -> None:
    outcome = evaluate(
        tenant_config,
        make_proposal(),
        action="approval_action",
    )

    assert_outcome(
        outcome,
        status=RoutingStatus.PENDING_APPROVAL,
        decision=RoutingDecision.NEEDS_APPROVAL,
        reason="approval_required",
    )


def test_critical_priority_requires_approval_for_registered_create_case(
    tenant_config: TenantConfig,
) -> None:
    outcome = evaluate(
        tenant_config,
        make_proposal(priority=ProposalPriority.CRITICAL),
        action="create_case",
    )

    assert_outcome(
        outcome,
        status=RoutingStatus.PENDING_APPROVAL,
        decision=RoutingDecision.NEEDS_APPROVAL,
        reason="approval_required",
    )


def test_incomplete_critical_create_case_remains_denied(
    tenant_config: TenantConfig,
) -> None:
    outcome = evaluate(
        tenant_config,
        make_proposal(field_names=(), priority=ProposalPriority.CRITICAL),
        action="create_case",
    )

    assert_outcome(
        outcome,
        status=RoutingStatus.AWAITING_INPUT,
        decision=RoutingDecision.DENY,
        reason="missing_required_fields",
        missing=("summary",),
    )


def test_trusted_risk_remains_urgent_for_critical_proposal(
    tenant_config: TenantConfig,
) -> None:
    outcome = evaluate(
        tenant_config,
        make_proposal(priority=ProposalPriority.CRITICAL),
        action="create_case",
        safety_or_legal_risk=True,
    )

    assert_outcome(
        outcome,
        status=RoutingStatus.URGENT,
        decision=RoutingDecision.NEEDS_APPROVAL,
        reason="safety_or_legal_risk",
    )


def test_complete_create_case_is_ready_and_allowed(
    tenant_config: TenantConfig,
) -> None:
    outcome = evaluate(
        tenant_config,
        make_proposal(),
        action="create_case",
    )

    assert_outcome(
        outcome,
        status=RoutingStatus.READY,
        decision=RoutingDecision.ALLOW,
        reason="action_allowed",
    )


def test_high_confidence_cannot_change_deny_or_review(
    tenant_config: TenantConfig,
) -> None:
    denied_low = evaluate(
        tenant_config,
        make_proposal(intake_type="unknown", field_names=(), confidence=0.0),
        action="create_case",
    )
    denied_high = evaluate(
        tenant_config,
        make_proposal(intake_type="unknown", field_names=(), confidence=1.0),
        action="create_case",
    )
    reviewed_low = evaluate(
        tenant_config,
        make_proposal(confidence=0.0),
        action="create_case",
        safety_or_legal_risk=True,
    )
    reviewed_high = evaluate(
        tenant_config,
        make_proposal(confidence=1.0),
        action="create_case",
        safety_or_legal_risk=True,
    )

    assert (denied_low.status, denied_low.decision, denied_low.reason) == (
        denied_high.status,
        denied_high.decision,
        denied_high.reason,
    )
    assert (reviewed_low.status, reviewed_low.decision, reviewed_low.reason) == (
        reviewed_high.status,
        reviewed_high.decision,
        reviewed_high.reason,
    )
    assert denied_high.decision is RoutingDecision.DENY
    assert reviewed_high.decision is RoutingDecision.NEEDS_APPROVAL


def test_missing_required_fields_are_recomputed_not_taken_from_proposal(
    tenant_config: TenantConfig,
) -> None:
    incomplete = evaluate(
        tenant_config,
        make_proposal(field_names=("unrecognized",), missing_required_fields=[]),
        action="create_case",
    )
    complete = evaluate(
        tenant_config,
        make_proposal(
            field_names=("summary",),
            missing_required_fields=["summary"],
        ),
        action="create_case",
    )

    assert_outcome(
        incomplete,
        status=RoutingStatus.AWAITING_INPUT,
        decision=RoutingDecision.DENY,
        reason="missing_required_fields",
        missing=("summary",),
    )
    assert_outcome(
        complete,
        status=RoutingStatus.READY,
        decision=RoutingDecision.ALLOW,
        reason="action_allowed",
    )


@pytest.mark.parametrize(
    "value",
    [None, "", []],
    ids=["null", "blank_string", "empty_list"],
)
def test_empty_required_values_are_missing_even_when_model_claims_complete(
    tenant_config: TenantConfig,
    value: object,
) -> None:
    outcome = evaluate(
        tenant_config,
        make_proposal(
            field_values=(("summary", value),),
            missing_required_fields=[],
        ),
        action="create_case",
    )

    assert_outcome(
        outcome,
        status=RoutingStatus.AWAITING_INPUT,
        decision=RoutingDecision.DENY,
        reason="missing_required_fields",
        missing=("summary",),
    )
