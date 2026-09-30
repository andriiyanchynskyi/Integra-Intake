"""Human approval decision contracts and the locked decision service."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_keys import AuthenticatedOperator
from app.db.models import Approval
from app.domain.approval_repository import (
    ApprovalCreationConflict,
    ApprovalObservationIdentity,
    ApprovalRepository,
)
from app.observability import (
    Component,
    EventLevel,
    EventName,
    NULL_OBSERVER,
    ObservationContext,
    ObservationEvent,
    Observer,
    OutcomeCode,
    safe_emit,
)
from app.tools.models import PendingAction
from app.tools.registry import (
    ActionCapabilityUnavailable,
    ActionRegistry,
    BUILTIN_ACTION_REGISTRY,
)
from app.runtime.profiles import (
    ResolvedTenantProfile,
    TenantProfileUnavailableError,
    resolve_persisted_profile,
)


class ApprovalNotFound(LookupError):
    """The approval is absent or belongs to another tenant."""


class ApprovalDecisionConflict(ValueError):
    """The requested decision cannot change the durable approval state."""


class ApprovalExpired(ApprovalDecisionConflict):
    """An approve request arrived at or after the approval deadline."""


class ApprovalActionExecutor(Protocol):
    async def execute(
        self,
        session: AsyncSession,
        approval: Approval,
        action: PendingAction,
        *,
        now: datetime,
    ) -> dict[str, object]: ...


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class ApprovalDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    approval_id: UUID
    status: Literal["approved", "rejected", "expired"]
    action: str
    decided_at: datetime
    case_id: UUID | None = None
    executed: bool


class ApprovalDecisionService:
    def __init__(
        self,
        session: AsyncSession,
        action_executor: ApprovalActionExecutor,
        *,
        clock: Callable[[], datetime] | None = None,
        observer: Observer = NULL_OBSERVER,
        request_id: UUID | None = None,
        action_registry: ActionRegistry = BUILTIN_ACTION_REGISTRY,
    ) -> None:
        self.session = session
        self.repository = ApprovalRepository(session)
        self.action_executor = action_executor
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.observer = observer
        self.request_id = request_id
        self.action_registry = action_registry

    async def decide(
        self,
        approval_id: UUID,
        operator: AuthenticatedOperator,
        request: ApprovalDecisionRequest,
    ) -> ApprovalDecisionResponse:
        now = self._now()
        expired_approval = False
        response: ApprovalDecisionResponse | None = None
        identity: ApprovalObservationIdentity | None = None
        resolved_profile: ResolvedTenantProfile | None = None
        decision_outcome: OutcomeCode | None = None
        action_executed = False
        decision_latency_ms: int | None = None
        async with self.session.begin():
            approval = await self.repository.get_for_tenant_for_update(
                approval_id, operator.tenant_id
            )
            if approval is None:
                raise ApprovalNotFound
            if (
                approval.job_id is None
                or approval.pending_action is None
                or approval.tenant_config_sha256 is None
                or approval.expires_at is None
            ):
                raise ApprovalDecisionConflict("legacy approval is not executable")
            try:
                identity = await self.repository.get_job_observation_identity(approval)
                if identity is None:
                    raise ApprovalCreationConflict("approval job is unavailable")
                try:
                    resolved_profile = resolve_persisted_profile(
                        identity.tenant_config_snapshot,
                        identity.tenant_config_sha256,
                    )
                except (TenantProfileUnavailableError, ValueError) as error:
                    raise ApprovalDecisionConflict(
                        "approval trusted profile is unavailable"
                    ) from error
            except ApprovalCreationConflict as error:
                raise ApprovalDecisionConflict("approval job is unavailable") from error
            decision_latency_ms = self._decision_latency_ms(approval, now)

            if approval.status in {"approved", "rejected", "expired"}:
                if self._is_same_final_decision(approval, request.decision):
                    response = self._response(approval)
                    decision_outcome = OutcomeCode.REUSED
                else:
                    raise ApprovalDecisionConflict("approval already decided")
            elif approval.status != "pending":
                raise ApprovalDecisionConflict("approval state is not decidable")
            elif approval.expires_at <= now:
                self.repository.expire_locked(approval, now=now)
                decision_outcome = OutcomeCode.EXPIRED
                if request.decision == "reject":
                    response = self._response(approval)
                else:
                    # Commit the timeout transition before reporting the approve
                    # conflict. Raising inside this transaction would roll it back
                    # and leave a deadline-past row pending.
                    expired_approval = True
            elif request.decision == "reject":
                approval.status = "rejected"
                approval.decided_at = now
                approval.decided_by_actor_ref = operator.actor_ref
                approval.decision_reason = request.reason
                approval.decision = {
                    "decision": "reject",
                    "reason": request.reason,
                }
                self.repository.add_event(
                    approval.tenant_id,
                    approval.id,
                    event_type="approval_rejected",
                    actor_ref=operator.actor_ref,
                    payload={},
                )
                response = self._response(approval)
                decision_outcome = OutcomeCode.REJECTED
            else:
                try:
                    action = PendingAction.model_validate(approval.pending_action)
                except ValidationError as error:
                    raise ApprovalDecisionConflict("approval action is invalid") from error
                if action.name != approval.action:
                    raise ApprovalDecisionConflict("approval action is invalid")
                try:
                    capability = self.action_registry.require(action.name)
                except ActionCapabilityUnavailable as error:
                    raise ApprovalDecisionConflict("approval action is invalid") from error
                if action.version != capability.pending_action_version:
                    raise ApprovalDecisionConflict("approval action is invalid")
                try:
                    capability.arguments_model.model_validate(action.arguments)
                except ValidationError as error:
                    raise ApprovalDecisionConflict("approval action is invalid") from error
                execution_result = await self.action_executor.execute(
                    self.session,
                    approval,
                    action,
                    now=now,
                )
                approval.status = "approved"
                approval.decided_at = now
                approval.decided_by_actor_ref = operator.actor_ref
                approval.decision_reason = request.reason
                approval.executed_at = now
                approval.execution_result = execution_result
                approval.decision = {
                    "decision": "approve",
                    "reason": request.reason,
                }
                self.repository.add_event(
                    approval.tenant_id,
                    approval.id,
                    event_type="approval_approved",
                    actor_ref=operator.actor_ref,
                    payload={},
                )
                self.repository.add_event(
                    approval.tenant_id,
                    approval.id,
                    event_type="approved_action_executed",
                    actor_ref=operator.actor_ref,
                    payload={
                        "action": approval.action,
                        "executed": True,
                    },
                )
                response = self._response(approval)
                decision_outcome = OutcomeCode.APPROVED
                action_executed = True

        if identity is not None and decision_outcome is not None:
            assert resolved_profile is not None
            self._emit_decision(
                approval=approval,
                identity=identity,
                profile=resolved_profile,
                outcome=decision_outcome,
                duration_ms=decision_latency_ms,
            )
            if decision_outcome is OutcomeCode.EXPIRED:
                self._emit_expired(
                    approval=approval,
                    identity=identity,
                    profile=resolved_profile,
                    duration_ms=decision_latency_ms,
                )
            if action_executed:
                self._emit_action_completed(
                    approval=approval,
                    identity=identity,
                    profile=resolved_profile,
                    duration_ms=decision_latency_ms,
                )
        if expired_approval:
            raise ApprovalExpired("approval expired")
        if response is not None:
            return response
        raise ApprovalDecisionConflict("approval decision did not complete")

    def _emit_decision(
        self,
        *,
        approval: Approval,
        identity: ApprovalObservationIdentity,
        profile: ResolvedTenantProfile,
        outcome: OutcomeCode,
        duration_ms: int | None,
    ) -> None:
        self._emit_approval_event(
            event=EventName.APPROVAL_DECIDED,
            approval=approval,
            identity=identity,
            profile=profile,
            outcome=outcome,
            duration_ms=duration_ms,
        )

    def _emit_expired(
        self,
        *,
        approval: Approval,
        identity: ApprovalObservationIdentity,
        profile: ResolvedTenantProfile,
        duration_ms: int | None,
    ) -> None:
        self._emit_approval_event(
            event=EventName.APPROVAL_EXPIRED,
            approval=approval,
            identity=identity,
            profile=profile,
            outcome=OutcomeCode.EXPIRED,
            duration_ms=duration_ms,
        )

    def _emit_action_completed(
        self,
        *,
        approval: Approval,
        identity: ApprovalObservationIdentity,
        profile: ResolvedTenantProfile,
        duration_ms: int | None,
    ) -> None:
        self._emit_approval_event(
            event=EventName.APPROVAL_ACTION_COMPLETED,
            approval=approval,
            identity=identity,
            profile=profile,
            outcome=OutcomeCode.EXECUTED,
            duration_ms=duration_ms,
        )

    def _emit_approval_event(
        self,
        *,
        event: EventName,
        approval: Approval,
        identity: ApprovalObservationIdentity,
        profile: ResolvedTenantProfile,
        outcome: OutcomeCode,
        duration_ms: int | None,
    ) -> None:
        try:
            self.action_registry.require(approval.action)
        except ActionCapabilityUnavailable:
            action_key = None
            action_known = False
        else:
            action_key = approval.action
            action_known = True
        context = ObservationContext(
            trace_id=identity.trace_id,
            request_id=self.request_id,
            tenant_id=approval.tenant_id,
            job_id=approval.job_id,
            approval_id=approval.id,
            scenario_key=(
                profile.compiled.scenario_key
                if profile.compiled is not None
                else None
            ),
            profile_fingerprint=(
                profile.compiled.profile_fingerprint
                if profile.compiled is not None
                else None
            ),
        )
        safe_emit(
            self.observer,
            ObservationEvent(
                event=event,
                level=EventLevel.INFO,
                trace_id=context.trace_id,
                request_id=context.request_id,
                tenant_id=context.tenant_id,
                job_id=context.job_id,
                approval_id=context.approval_id,
                component=Component.APPROVAL,
                outcome=outcome,
                duration_ms=duration_ms,
                action_key=action_key,
                action_known=action_known,
                scenario_key=context.scenario_key,
                profile_fingerprint=context.profile_fingerprint,
            ),
        )

    @staticmethod
    def _decision_latency_ms(approval: Approval, now: datetime) -> int | None:
        if approval.created_at is None:
            return None
        created = approval.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        current = now
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return max(0, int((current - created).total_seconds() * 1000))

    @staticmethod
    def _is_same_final_decision(
        approval: Approval, decision: Literal["approve", "reject"]
    ) -> bool:
        if approval.status == "approved":
            return decision == "approve"
        if approval.status in {"rejected", "expired"}:
            return decision == "reject"
        return False

    @staticmethod
    def _response(approval: Approval) -> ApprovalDecisionResponse:
        if approval.decided_at is None:
            raise ApprovalDecisionConflict("approval decision timestamp is missing")
        return ApprovalDecisionResponse(
            approval_id=approval.id,
            status=approval.status,
            action=approval.action,
            decided_at=approval.decided_at,
            case_id=approval.case_id,
            executed=approval.executed_at is not None,
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


__all__ = [
    "ApprovalActionExecutor",
    "ApprovalDecisionConflict",
    "ApprovalDecisionRequest",
    "ApprovalDecisionResponse",
    "ApprovalDecisionService",
    "ApprovalExpired",
    "ApprovalNotFound",
]
