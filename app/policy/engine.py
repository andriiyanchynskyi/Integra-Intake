"""Pure deterministic policy evaluation after an LLM proposal."""

from app.agent.models import ProposalPriority
from app.policy.models import (
    PolicyInput,
    PolicyOutcome,
    proposal_value_is_present,
)
from app.tenants.routing import RoutingAssessment, TenantRouter


class PolicyEngine:
    """Adapt a validated proposal and trusted context to TenantRouter."""

    def evaluate(self, value: PolicyInput) -> PolicyOutcome:
        known_fields = set(value.runtime.tenant_config.fields)
        present_fields = frozenset(
            item.name
            for item in value.proposal.fields
            if item.name in known_fields and proposal_value_is_present(item.value)
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
            registered_actions=value.registered_actions,
            is_critical=value.proposal.priority is ProposalPriority.CRITICAL,
        )
        routed = TenantRouter(value.runtime.tenant_config).route(assessment)
        return PolicyOutcome(
            decision=routed.decision,
            status=routed.status,
            reason=routed.reason,
            missing_required_fields=routed.missing_required_fields,
        )
