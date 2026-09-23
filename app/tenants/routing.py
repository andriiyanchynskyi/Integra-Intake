"""Pure deterministic routing for validated tenant intake profiles."""

from dataclasses import dataclass

from app.tenants.config import RoutingDecision, RoutingStatus, TenantConfig


@dataclass(frozen=True, slots=True)
class RoutingAssessment:
    intake_type: str
    present_fields: frozenset[str]
    contains_safety_or_legal_risk: bool
    requested_action: str
    requires_complete_fields: bool = True
    registered_actions: frozenset[str] | None = None
    is_critical: bool = False


@dataclass(frozen=True, slots=True)
class RoutingOutcome:
    status: RoutingStatus
    decision: RoutingDecision
    missing_required_fields: tuple[str, ...]
    reason: str


class TenantRouter:
    """Apply the profile's policy after another component supplies an assessment."""

    def __init__(self, config: TenantConfig) -> None:
        self.config = config
        self._intake_types = {item.name: item for item in config.intake_types}

    def route(self, assessment: RoutingAssessment) -> RoutingOutcome:
        if assessment.contains_safety_or_legal_risk:
            return RoutingOutcome(
                status=RoutingStatus.URGENT,
                decision=RoutingDecision.NEEDS_APPROVAL,
                missing_required_fields=(),
                reason="safety_or_legal_risk",
            )

        intake = self._intake_types.get(assessment.intake_type)
        if intake is None:
            return RoutingOutcome(
                status=RoutingStatus.REJECTED,
                decision=RoutingDecision.DENY,
                missing_required_fields=(),
                reason="unknown_intake_type",
            )

        missing = tuple(sorted(set(intake.required_fields) - assessment.present_fields))
        if assessment.requires_complete_fields and missing:
            return RoutingOutcome(
                status=RoutingStatus.AWAITING_INPUT,
                decision=RoutingDecision.DENY,
                missing_required_fields=missing,
                reason="missing_required_fields",
            )

        if (
            assessment.registered_actions is not None
            and assessment.requested_action not in assessment.registered_actions
        ):
            return RoutingOutcome(
                status=RoutingStatus.REJECTED,
                decision=RoutingDecision.DENY,
                missing_required_fields=(),
                reason="action_not_configured",
            )

        rule = self.config.action_policy.get(assessment.requested_action)
        if rule is None:
            return RoutingOutcome(
                status=RoutingStatus.REJECTED,
                decision=RoutingDecision.DENY,
                missing_required_fields=(),
                reason="action_not_configured",
            )
        if not rule.allowed:
            return RoutingOutcome(
                status=RoutingStatus.REJECTED,
                decision=RoutingDecision.DENY,
                missing_required_fields=(),
                reason="action_not_allowed",
            )

        if (
            assessment.requested_action in self.config.routing.always_approval_actions
            or rule.requires_approval
            or assessment.is_critical
        ):
            return RoutingOutcome(
                status=RoutingStatus.PENDING_APPROVAL,
                decision=RoutingDecision.NEEDS_APPROVAL,
                missing_required_fields=(),
                reason="approval_required",
            )

        return RoutingOutcome(
            status=RoutingStatus.READY,
            decision=RoutingDecision.ALLOW,
            missing_required_fields=(),
            reason="action_allowed",
        )
