"""Async PostgreSQL implementation behind the synchronous tool-port contract."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import threading
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import ResultReference, ToolData
from app.core.config import Settings, settings
from app.db.models import AgentJob, Approval, IdempotencyRecord
from app.domain.approval_repository import ApprovalRepository
from app.domain.artifact_repository import ArtifactRepository
from app.domain.repositories import CaseRepository
from app.domain.service import CaseService
from app.policy import TrustedSource, trusted_source_from_snapshot
from app.runtime.gateway import WorkerAsyncGateway
from app.tools.models import (
    ApprovalRequested,
    CreateCaseArgs,
    CreateReplyDraftArgs,
    CreatedCase,
    FlagForReviewArgs,
    CustomerSummary,
    FindCustomerArgs,
    PendingAction,
    UpdateCaseFieldsArgs,
    UpdatedCase,
)
from app.tools.ports import CustomerNotFoundError, TenantToolPort
from app.tools.registry import (
    ActionCapabilityUnavailable,
    ActionRegistry,
    BUILTIN_ACTION_REGISTRY,
)


class PostgresTenantToolPort:
    """Async, job-bound persistence operations with explicit tenant predicates."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        job_id: UUID,
        attempt_count: int | None = None,
        runtime_settings: Settings | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._job_id = job_id
        self._attempt_count = attempt_count
        self._settings = runtime_settings or settings
        self._cancellation_event = cancellation_event

    def _check_cancelled(self) -> None:
        if self._cancellation_event is not None and self._cancellation_event.is_set():
            raise RuntimeError("worker execution cancelled")

    async def find_customer(
        self,
        tenant_id: UUID,
        *,
        email: str | None,
        external_id: str | None,
    ) -> CustomerSummary | None:
        self._check_cancelled()
        async with self._session_factory() as session:
            customer = await CaseRepository(session).find_customer_for_tenant(
                tenant_id,
                email=email,
                external_id=external_id,
            )
            if customer is None:
                return None
            result = CustomerSummary(
                id=str(customer.id),
                external_id=customer.external_id,
                name=customer.name,
                email=customer.email,
                phone=customer.phone,
            )
            self._check_cancelled()
            return result

    async def case_exists(self, tenant_id: UUID, case_id: UUID) -> bool:
        self._check_cancelled()
        async with self._session_factory() as session:
            case = await CaseRepository(session).get_for_tenant(case_id, tenant_id)
            result = case is not None
            self._check_cancelled()
            return result

    async def request_approval(
        self,
        tenant_id: UUID,
        *,
        action: PendingAction,
        policy_reason: str,
    ) -> ApprovalRequested:
        self._check_cancelled()
        async with self._session_factory() as session:
            return await ApprovalRepository(session).create_for_running_job(
                tenant_id,
                self._job_id,
                attempt_count=self._attempt_count or 0,
                action=action,
                policy_reason=policy_reason,
                expires_in=timedelta(seconds=self._settings.approval_timeout_seconds),
                now=datetime.now(timezone.utc),
            )

    async def create_case(
        self,
        tenant_id: UUID,
        source: TrustedSource,
        *,
        customer_id: UUID | None,
        fields: Mapping[str, ToolData],
    ) -> CreatedCase:
        self._check_cancelled()
        async with self._session_factory() as session:
            async with session.begin():
                repository = CaseRepository(session)
                idempotency = await self._lock_idempotency(session, tenant_id)
                if idempotency.case_id is not None:
                    existing = await repository.get_for_tenant(
                        idempotency.case_id, tenant_id
                    )
                    if existing is None:
                        raise RuntimeError("job case is unavailable")
                    return CreatedCase(
                        id=str(existing.id),
                        status=existing.status,
                        accepted_fields=sorted(existing.extracted_fields),
                    )
                if customer_id is not None:
                    customer = await repository.get_customer_for_tenant(
                        customer_id, tenant_id
                    )
                    if customer is None:
                        raise CustomerNotFoundError
                case = await CaseService(session).create_agent_case(
                    tenant_id,
                    source,
                    customer_id=customer_id,
                    fields=fields,
                )
                await session.flush()
                CaseService(session).append_event(
                    tenant_id,
                    case.id,
                    event_type="created",
                    payload={},
                )
                idempotency.case_id = case.id
                self._check_cancelled()
                await self._mark_side_effect(session, tenant_id)
                return CreatedCase(
                    id=str(case.id),
                    status=case.status,
                    accepted_fields=sorted(fields),
                )

    async def create_reply_draft(
        self,
        tenant_id: UUID,
        *,
        job_id: UUID | None,
        recipient: str | None,
        subject: str,
        body: str,
    ) -> ResultReference:
        self._check_cancelled()
        if job_id is not None and job_id != self._job_id:
            raise RuntimeError("artifact job is unavailable")
        async with self._session_factory() as session:
            async with session.begin():
                reference = await ArtifactRepository(session).create_for_job(
                    tenant_id,
                    self._job_id,
                    kind="reply_draft",
                    payload={
                        "recipient": recipient,
                        "subject": subject,
                        "body": body,
                    },
                )
                self._check_cancelled()
                await self._mark_side_effect(session, tenant_id)
                return reference

    async def flag_for_review(
        self,
        tenant_id: UUID,
        *,
        job_id: UUID | None,
        reason: str,
    ) -> ResultReference:
        self._check_cancelled()
        if job_id is not None and job_id != self._job_id:
            raise RuntimeError("artifact job is unavailable")
        async with self._session_factory() as session:
            async with session.begin():
                reference = await ArtifactRepository(session).create_for_job(
                    tenant_id,
                    self._job_id,
                    kind="review_flag",
                    payload={"reason": reason},
                )
                self._check_cancelled()
                await self._mark_side_effect(session, tenant_id)
                return reference

    async def update_case_fields(
        self,
        tenant_id: UUID,
        case_id: UUID,
        *,
        fields: Mapping[str, ToolData],
    ) -> UpdatedCase | None:
        self._check_cancelled()
        async with self._session_factory() as session:
            async with session.begin():
                repository = CaseRepository(session)
                case = await repository.get_for_tenant_for_update(case_id, tenant_id)
                if case is None:
                    return None
                merged = deepcopy(dict(case.extracted_fields or {}))
                merged.update(deepcopy(dict(fields)))
                case.extracted_fields = merged
                self._check_cancelled()
                CaseService(session).append_event(
                    tenant_id,
                    case.id,
                    event_type="fields_updated",
                    payload={"updated_fields": sorted(fields)},
                )
                await self._mark_side_effect(session, tenant_id)
                return UpdatedCase(
                    id=str(case.id),
                    updated_fields=sorted(fields),
                )

    async def _lock_idempotency(
        self, session: AsyncSession, tenant_id: UUID
    ) -> IdempotencyRecord:
        statement = (
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.tenant_id == tenant_id,
                IdempotencyRecord.job_id == self._job_id,
            )
            .with_for_update()
        )
        record = (await session.execute(statement)).scalar_one_or_none()
        if record is None:
            raise RuntimeError("job idempotency record is unavailable")
        return record

    async def _mark_side_effect(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._check_cancelled()
        conditions = [
            AgentJob.id == self._job_id,
            AgentJob.tenant_id == tenant_id,
        ]
        if self._attempt_count is not None:
            conditions.extend(
                [
                    AgentJob.status == "running",
                    AgentJob.attempt_count == self._attempt_count,
                ]
            )
        statement = select(AgentJob).where(*conditions).with_for_update()
        job = (await session.execute(statement)).scalar_one_or_none()
        if job is None:
            raise RuntimeError("agent job is unavailable")
        self._check_cancelled()
        job.side_effect_committed_at = datetime.now(timezone.utc)


class SyncTenantToolPort(TenantToolPort):
    """Synchronous facade used only by the agent thread."""

    def __init__(
        self,
        gateway: WorkerAsyncGateway,
        async_port: PostgresTenantToolPort,
    ) -> None:
        self._gateway = gateway
        self._async_port = async_port

    def _check_cancelled(self) -> None:
        checker = getattr(self._async_port, "_check_cancelled", None)
        if callable(checker):
            checker()

    def find_customer(
        self,
        tenant_id: UUID,
        *,
        email: str | None,
        external_id: str | None,
    ) -> CustomerSummary | None:
        self._check_cancelled()
        result = self._gateway.call(
            self._async_port.find_customer(
                tenant_id, email=email, external_id=external_id
            )
        )
        self._check_cancelled()
        return result

    def create_case(
        self,
        tenant_id: UUID,
        source: TrustedSource,
        *,
        customer_id: UUID | None,
        fields: Mapping[str, ToolData],
    ) -> CreatedCase:
        self._check_cancelled()
        result = self._gateway.call(
            self._async_port.create_case(
                tenant_id,
                source,
                customer_id=customer_id,
                fields=fields,
            )
        )
        self._check_cancelled()
        return result

    def update_case_fields(
        self,
        tenant_id: UUID,
        case_id: UUID,
        *,
        fields: Mapping[str, ToolData],
    ) -> UpdatedCase | None:
        self._check_cancelled()
        result = self._gateway.call(
            self._async_port.update_case_fields(
                tenant_id,
                case_id,
                fields=fields,
            )
        )
        self._check_cancelled()
        return result

    def case_exists(self, tenant_id: UUID, case_id: UUID) -> bool:
        self._check_cancelled()
        result = self._gateway.call(
            self._async_port.case_exists(tenant_id, case_id)
        )
        self._check_cancelled()
        return result

    def request_approval(
        self,
        tenant_id: UUID,
        *,
        action: PendingAction,
        policy_reason: str,
    ) -> ApprovalRequested:
        self._check_cancelled()
        result = self._gateway.call(
            self._async_port.request_approval(
                tenant_id,
                action=action,
                policy_reason=policy_reason,
            )
        )
        self._check_cancelled()
        return result

    def create_reply_draft(
        self,
        tenant_id: UUID,
        *,
        job_id: UUID | None,
        recipient: str | None,
        subject: str,
        body: str,
    ) -> ResultReference:
        self._check_cancelled()
        result = self._gateway.call(
            self._async_port.create_reply_draft(
                tenant_id,
                job_id=job_id,
                recipient=recipient,
                subject=subject,
                body=body,
            )
        )
        self._check_cancelled()
        return result

    def flag_for_review(
        self,
        tenant_id: UUID,
        *,
        job_id: UUID | None,
        reason: str,
    ) -> ResultReference:
        self._check_cancelled()
        result = self._gateway.call(
            self._async_port.flag_for_review(
                tenant_id,
                job_id=job_id,
                reason=reason,
            )
        )
        self._check_cancelled()
        return result


class PostgresApprovalActionExecutor:
    """Execute one frozen approval command in the caller's transaction."""

    def __init__(
        self,
        action_registry: ActionRegistry = BUILTIN_ACTION_REGISTRY,
    ) -> None:
        self._action_registry = action_registry

    async def execute(
        self,
        session: AsyncSession,
        approval: Approval,
        action: PendingAction,
        *,
        now: datetime,
    ) -> dict[str, object]:
        if approval.job_id is None or approval.tenant_config_sha256 is None:
            raise RuntimeError("approval trusted context is unavailable")
        job_statement = (
            select(AgentJob)
            .where(
                AgentJob.id == approval.job_id,
                AgentJob.tenant_id == approval.tenant_id,
            )
            .with_for_update()
        )
        job = (await session.execute(job_statement)).scalar_one_or_none()
        if (
            job is None
            or job.status != "awaiting_approval"
            or job.tenant_config_sha256 != approval.tenant_config_sha256
            or approval.action != action.name
        ):
            raise RuntimeError("approval trusted job is unavailable")

        try:
            capability = self._action_registry.require(action.name)
        except ActionCapabilityUnavailable as error:
            raise RuntimeError("approval action is not registered") from error
        if action.version != capability.pending_action_version:
            raise RuntimeError("approval action version is unavailable")
        try:
            capability.arguments_model.model_validate(action.arguments)
        except Exception as error:
            raise RuntimeError("approved action arguments are invalid") from error

        handlers = {
            "create_case": self._create_case,
            "update_case_fields": self._update_case_fields,
            "find_customer": self._find_customer,
            "create_reply_draft": self._create_reply_draft,
            "flag_for_review": self._flag_for_review,
        }
        if frozenset(handlers) != self._action_registry.keys:
            raise RuntimeError("action registry and approval handlers are incompatible")
        handler = handlers.get(capability.key)
        if handler is None:
            raise RuntimeError("approval action is not registered")
        result = await handler(session, approval, job, action)
        if capability.commits_side_effect:
            job.side_effect_committed_at = now
        case_id = result.get("case_id")
        if isinstance(case_id, str):
            try:
                approval.case_id = UUID(case_id)
            except ValueError as error:
                raise RuntimeError("approved case result is invalid") from error
        return result

    async def _find_customer(
        self,
        session: AsyncSession,
        approval: Approval,
        job: AgentJob,
        action: PendingAction,
    ) -> dict[str, object]:
        del job
        values = FindCustomerArgs.model_validate(action.arguments)
        customer = await CaseRepository(session).find_customer_for_tenant(
            approval.tenant_id,
            email=values.email,
            external_id=values.external_id,
        )

        return {
            "found": customer is not None,
            "customer_id": str(customer.id) if customer is not None else None,
        }

    async def _create_reply_draft(
        self,
        session: AsyncSession,
        approval: Approval,
        job: AgentJob,
        action: PendingAction,
    ) -> dict[str, object]:
        values = CreateReplyDraftArgs.model_validate(action.arguments)
        if values.case_id is not None:
            case = await CaseRepository(session).get_for_tenant(
                values.case_id, approval.tenant_id
            )
            if case is None:
                raise RuntimeError("approved case is unavailable")
            approval.case_id = case.id
        source = self._source_from_job(job.source_snapshot)
        recipient = action.known_fields.get("contact")
        if not isinstance(recipient, str):
            recipient = None
        reference = await ArtifactRepository(session).create_for_job(
            approval.tenant_id,
            job.id,
            kind="reply_draft",
            payload={
                "recipient": recipient,
                "subject": source.subject or "Intake reply",
                "body": "Reply draft generated for operator review.",
            },
        )
        return {
            "result_reference": reference.model_dump(mode="json"),
            "persisted": True,
        }

    async def _flag_for_review(
        self,
        session: AsyncSession,
        approval: Approval,
        job: AgentJob,
        action: PendingAction,
    ) -> dict[str, object]:
        values = FlagForReviewArgs.model_validate(action.arguments)
        reference = await ArtifactRepository(session).create_for_job(
            approval.tenant_id,
            job.id,
            kind="review_flag",
            payload={
                "reason": values.note or "Review requested for operator follow-up.",
            },
        )
        return {
            "result_reference": reference.model_dump(mode="json"),
            "persisted": True,
        }

    async def _create_case(
        self,
        session: AsyncSession,
        approval: Approval,
        job: AgentJob,
        action: PendingAction,
    ) -> dict[str, object]:
        idempotency_statement = (
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.tenant_id == approval.tenant_id,
                IdempotencyRecord.job_id == job.id,
            )
            .with_for_update()
        )
        idempotency = (
            await session.execute(idempotency_statement)
        ).scalar_one_or_none()
        if idempotency is None:
            raise RuntimeError("approval job idempotency record is unavailable")
        repository = CaseRepository(session)
        if idempotency.case_id is not None:
            existing = await repository.get_for_tenant(
                idempotency.case_id, approval.tenant_id
            )
            if existing is None:
                raise RuntimeError("approved case is unavailable")
            return {
                "case_id": str(existing.id),
                "status": existing.status,
                "accepted_fields": sorted(existing.extracted_fields),
            }

        values = CreateCaseArgs.model_validate(action.arguments)
        if values.customer_id is not None:
            customer = await repository.get_customer_for_tenant(
                values.customer_id, approval.tenant_id
            )
            if customer is None:
                raise RuntimeError("approved customer is unavailable")
        source = self._source_from_job(job.source_snapshot)
        case = await CaseService(session).create_agent_case(
            approval.tenant_id,
            source,
            customer_id=values.customer_id,
            fields=deepcopy(dict(action.known_fields)),
        )
        await session.flush()
        CaseService(session).append_event(
            approval.tenant_id,
            case.id,
            event_type="created",
            payload={},
        )
        idempotency.case_id = case.id
        return {
            "case_id": str(case.id),
            "status": case.status,
            "accepted_fields": sorted(action.known_fields),
        }

    async def _update_case_fields(
        self,
        session: AsyncSession,
        approval: Approval,
        job: AgentJob,
        action: PendingAction,
    ) -> dict[str, object]:
        del job
        values = UpdateCaseFieldsArgs.model_validate(action.arguments)
        repository = CaseRepository(session)
        case = await repository.get_for_tenant_for_update(
            values.case_id, approval.tenant_id
        )
        if case is None:
            raise RuntimeError("approved case is unavailable")
        approval.case_id = case.id
        merged = deepcopy(dict(case.extracted_fields or {}))
        merged.update(deepcopy(dict(action.known_fields)))
        case.extracted_fields = merged
        CaseService(session).append_event(
            approval.tenant_id,
            case.id,
            event_type="fields_updated",
            payload={"updated_fields": sorted(action.known_fields)},
        )
        return {
            "case_id": str(case.id),
            "updated_fields": sorted(action.known_fields),
        }

    @staticmethod
    def _source_from_job(value: object) -> TrustedSource:
        try:
            return trusted_source_from_snapshot(value)
        except RuntimeError as error:
            raise RuntimeError("approved source snapshot is invalid") from error


__all__ = [
    "PostgresApprovalActionExecutor",
    "PostgresTenantToolPort",
    "SyncTenantToolPort",
]
