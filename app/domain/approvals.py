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
from app.domain.approval_repository import ApprovalRepository
from app.tools.models import PendingAction


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
    ) -> None:
        self.session = session
        self.repository = ApprovalRepository(session)
        self.action_executor = action_executor
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def decide(
        self,
        approval_id: UUID,
        operator: AuthenticatedOperator,
        request: ApprovalDecisionRequest,
    ) -> ApprovalDecisionResponse:
        now = self._now()
        expired_approval = False
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

            if approval.status in {"approved", "rejected", "expired"}:
                if self._is_same_final_decision(approval, request.decision):
                    return self._response(approval)
                raise ApprovalDecisionConflict("approval already decided")

            if approval.status != "pending":
                raise ApprovalDecisionConflict("approval state is not decidable")

            if approval.expires_at <= now:
                self.repository.expire_locked(approval, now=now)
                if request.decision == "reject":
                    return self._response(approval)
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
                return self._response(approval)

            if expired_approval:
                response = self._response(approval)
            else:
                try:
                    action = PendingAction.model_validate(approval.pending_action)
                except ValidationError as error:
                    raise ApprovalDecisionConflict("approval action is invalid") from error
                if action.name != approval.action:
                    raise ApprovalDecisionConflict("approval action is invalid")
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
                return self._response(approval)

        if expired_approval:
            raise ApprovalExpired("approval expired")
        raise ApprovalDecisionConflict("approval decision did not complete")

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
