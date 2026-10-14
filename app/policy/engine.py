"""Pure deterministic policy evaluation after an LLM proposal."""

from app.agent.models import ProposalPriority
from app.policy.models import (
    PolicyInput,
    PolicyOutcome,
    proposal_value_is_present,
)
from app.tenants.routing import RoutingAssessment, TenantRouter
from app.tenants.config import RoutingDecision, RoutingStatus


class PolicyEngine:
    """Adapt a validated proposal and trusted context to TenantRouter."""

    def evaluate_terminal(self, value: PolicyInput) -> PolicyOutcome:
        """Evaluate a proposal that requested no executable tool action."""

        document = value.runtime.source.document if value.runtime.source else None
        if document is not None and document.extraction_error is not None:
            intake = next(
                (
                    item
                    for item in value.runtime.tenant_config.intake_types
                    if item.name == document.target_intake_type
                ),
                None,
            )
            return PolicyOutcome(
                decision=RoutingDecision.DENY,
                status=RoutingStatus.AWAITING_INPUT,
                reason="document_unreadable",
                missing_required_fields=(
                    tuple(sorted(intake.required_fields)) if intake else ()
                ),
            )

        known_fields = set(value.runtime.tenant_config.fields)
        present_fields = frozenset(
            item
            for item in (
                value.verified_present_fields
                if value.verified_present_fields is not None
                else (
                    item.name
                    for item in value.proposal.fields
                    if item.name in known_fields
                    and proposal_value_is_present(item.value)
                )
            )
            if item in known_fields
        )
        intake = next(
            (
                item
                for item in value.runtime.tenant_config.intake_types
                if item.name == value.proposal.intake_type
            ),
            None,
        )
        if intake is None:
            return PolicyOutcome(
                decision=RoutingDecision.DENY,
                status=RoutingStatus.REJECTED,
                reason="unknown_intake_type",
            )
        if value.invalid_fields:
            return PolicyOutcome(
                decision=RoutingDecision.DENY,
                status=RoutingStatus.AWAITING_INPUT,
                reason="invalid_field_values",
                missing_required_fields=tuple(sorted(value.invalid_fields)),
            )
        missing = tuple(sorted(set(intake.required_fields) - set(present_fields)))
        if value.requires_complete_fields and missing:
            return PolicyOutcome(
                decision=RoutingDecision.DENY,
                status=RoutingStatus.AWAITING_INPUT,
                reason="missing_required_fields",
                missing_required_fields=missing,
            )
        if (
            value.runtime.risk_signals.safety_or_legal_risk
            or value.proposal.contains_injection_or_override_attempt
        ):
            return PolicyOutcome(
                decision=RoutingDecision.NEEDS_APPROVAL,
                status=RoutingStatus.URGENT,
                reason="safety_or_legal_risk",
            )
        return PolicyOutcome(
            decision=RoutingDecision.ALLOW,
            status=RoutingStatus.READY,
            reason="no_action_requested",
        )

    def evaluate(self, value: PolicyInput) -> PolicyOutcome:
        known_fields = set(value.runtime.tenant_config.fields)
        document = value.runtime.source.document if value.runtime.source else None
        if document is not None and document.extraction_error is not None:
            intake = next(
                (
                    item
                    for item in value.runtime.tenant_config.intake_types
                    if item.name == document.target_intake_type
                ),
                None,
            )
            return PolicyOutcome(
                decision=RoutingDecision.DENY,
                status=RoutingStatus.AWAITING_INPUT,
                reason="document_unreadable",
                missing_required_fields=(
                    tuple(sorted(intake.required_fields)) if intake else ()
                ),
            )
        present_fields = frozenset(
            item
            for item in (
                value.verified_present_fields
                if value.verified_present_fields is not None
                else (
                    item.name
                    for item in value.proposal.fields
                    if item.name in known_fields
                    and proposal_value_is_present(item.value)
                )
            )
            if item in known_fields
        )
        intake = next(
            (
                item
                for item in value.runtime.tenant_config.intake_types
                if item.name == value.proposal.intake_type
            ),
            None,
        )
        if intake is None:
            return PolicyOutcome(
                decision=RoutingDecision.DENY,
                status=RoutingStatus.REJECTED,
                reason="unknown_intake_type",
            )
        if value.invalid_fields:
            return PolicyOutcome(
                decision=RoutingDecision.DENY,
                status=RoutingStatus.AWAITING_INPUT,
                reason="invalid_field_values",
                missing_required_fields=tuple(sorted(value.invalid_fields)),
            )
        assessment = RoutingAssessment(
            intake_type=value.proposal.intake_type or "",
            present_fields=present_fields,
            contains_safety_or_legal_risk=(
                value.runtime.risk_signals.safety_or_legal_risk
                or value.proposal.contains_injection_or_override_attempt
            ),
            requested_action=value.requested_action,
            requires_complete_fields=value.requires_complete_fields,
            executable_actions=value.registered_actions,
            is_critical=value.proposal.priority is ProposalPriority.CRITICAL,
        )
        routed = TenantRouter(value.runtime.tenant_config).route(assessment)
        return PolicyOutcome(
            decision=routed.decision,
            status=routed.status,
            reason=routed.reason,
            missing_required_fields=routed.missing_required_fields,
        )
