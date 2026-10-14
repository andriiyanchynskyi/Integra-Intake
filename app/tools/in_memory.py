"""Deterministic tenant-scoped adapters used by the internal tool boundary."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.agent import ResultReference, ToolData
from app.policy import TrustedSource
from app.tools.models import (
    ApprovalRequested,
    CreatedCase,
    CustomerSummary,
    PendingAction,
    UpdatedCase,
)
from app.tools.ports import CustomerNotFoundError, _CaseRecord, _CustomerRecord


class InMemoryTenantToolPort:
    """A test/runtime double that enforces tenant predicates in every method."""

    def __init__(self) -> None:
        self.customers: dict[UUID, _CustomerRecord] = {}
        self.cases: dict[UUID, _CaseRecord] = {}
        self.approval_requests: list[tuple[UUID, PendingAction, str]] = []
        self.artifacts: dict[tuple[UUID, UUID, str], dict[str, object]] = {}
        self._default_job_id = uuid4()

    def add_customer(
        self,
        tenant_id: UUID,
        *,
        customer_id: UUID | None = None,
        external_id: str | None = None,
        name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
    ) -> CustomerSummary:
        summary = CustomerSummary(
            id=str(customer_id or uuid4()),
            external_id=external_id,
            name=name,
            email=email,
            phone=phone,
        )
        identifier = UUID(summary.id)
        self.customers[identifier] = _CustomerRecord(tenant_id, summary)
        return summary

    def find_customer(
        self,
        tenant_id: UUID,
        *,
        email: str | None,
        external_id: str | None,
    ) -> CustomerSummary | None:
        for record in self.customers.values():
            if record.tenant_id != tenant_id:
                continue
            if email is not None and record.summary.email == email:
                return deepcopy(record.summary)
            if (
                external_id is not None
                and record.summary.external_id == external_id
            ):
                return deepcopy(record.summary)
        return None

    def create_case(
        self,
        tenant_id: UUID,
        source: TrustedSource,
        *,
        customer_id: UUID | None,
        fields: Mapping[str, ToolData],
    ) -> CreatedCase:
        if customer_id is not None:
            customer = self.customers.get(customer_id)
            if customer is None or customer.tenant_id != tenant_id:
                raise CustomerNotFoundError
        case_id = uuid4()
        self.cases[case_id] = _CaseRecord(
            tenant_id=tenant_id,
            id=case_id,
            status="received",
            channel=source.channel,
            subject=source.subject,
            body=source.body,
            extracted_fields=deepcopy(dict(fields)),
        )
        return CreatedCase(
            id=str(case_id),
            status="received",
            accepted_fields=sorted(fields),
        )

    def seed_case(
        self,
        tenant_id: UUID,
        *,
        case_id: UUID | None = None,
        fields: Mapping[str, ToolData] | None = None,
    ) -> UUID:
        identifier = case_id or uuid4()
        self.cases[identifier] = _CaseRecord(
            tenant_id=tenant_id,
            id=identifier,
            status="received",
            channel="email",
            subject="Existing case",
            body="Existing body",
            extracted_fields=deepcopy(dict(fields or {})),
        )
        return identifier

    def update_case_fields(
        self,
        tenant_id: UUID,
        case_id: UUID,
        *,
        fields: Mapping[str, ToolData],
    ) -> UpdatedCase | None:
        record = self.cases.get(case_id)
        if record is None or record.tenant_id != tenant_id:
            return None
        record.extracted_fields.update(deepcopy(dict(fields)))
        return UpdatedCase(
            id=str(case_id),
            updated_fields=sorted(fields),
        )

    def case_exists(self, tenant_id: UUID, case_id: UUID) -> bool:
        record = self.cases.get(case_id)
        return record is not None and record.tenant_id == tenant_id

    def request_approval(
        self,
        tenant_id: UUID,
        *,
        action: PendingAction,
        policy_reason: str,
    ) -> ApprovalRequested:
        approval_id = uuid4()
        self.approval_requests.append((tenant_id, action, policy_reason))
        return ApprovalRequested(
            id=approval_id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )

    def create_reply_draft(
        self,
        tenant_id: UUID,
        *,
        job_id: UUID | None,
        recipient: str | None,
        subject: str,
        body: str,
    ) -> ResultReference:
        return self._create_artifact(
            tenant_id,
            job_id=job_id,
            kind="reply_draft",
            payload={
                "recipient": recipient,
                "subject": subject,
                "body": body,
            },
        )

    def flag_for_review(
        self,
        tenant_id: UUID,
        *,
        job_id: UUID | None,
        reason: str,
    ) -> ResultReference:
        return self._create_artifact(
            tenant_id,
            job_id=job_id,
            kind="review_flag",
            payload={"reason": reason},
        )

    def _create_artifact(
        self,
        tenant_id: UUID,
        *,
        job_id: UUID | None,
        kind: str,
        payload: dict[str, object],
    ) -> ResultReference:
        identifier = job_id or self._default_job_id
        key = (tenant_id, identifier, kind)
        existing = self.artifacts.get(key)
        if existing is not None:
            return ResultReference(kind=kind, id=existing["id"])  # type: ignore[arg-type]
        artifact_id = uuid4()
        self.artifacts[key] = {"id": artifact_id, "payload": deepcopy(payload)}
        return ResultReference(kind=kind, id=artifact_id)
