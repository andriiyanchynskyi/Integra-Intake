from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TimestampedModel:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Tenant(TimestampedModel, Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="active", server_default="active"
    )


class ApiKey(TimestampedModel, Base):
    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_tenant_id_id", "tenant_id", "id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    prefix: Mapped[str] = mapped_column(String(11), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class Customer(TimestampedModel, Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_customers_tenant_id_id"),
        Index("ix_customers_tenant_id_id", "tenant_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class IntakeCase(TimestampedModel, Base):
    __tablename__ = "intake_cases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_intake_cases_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            name="fk_intake_cases_tenant_customer",
        ),
        Index("ix_intake_cases_tenant_id_id", "tenant_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    customer_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="received", server_default="received"
    )
    channel: Mapped[str] = mapped_column(String(100), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(String(100))
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    extracted_fields: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class CaseEvent(Base):
    __tablename__ = "case_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["intake_cases.tenant_id", "intake_cases.id"],
            name="fk_case_events_tenant_case",
        ),
        Index("ix_case_events_tenant_id_id", "tenant_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    case_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(255))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentJob(TimestampedModel, Base):
    __tablename__ = "agent_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_agent_jobs_tenant_id_id"),
        Index("ix_agent_jobs_tenant_id_id", "tenant_id", "id"),
        Index("ix_agent_jobs_status_available_at", "status", "available_at"),
        Index("ix_agent_jobs_running_lease", "status", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="queued", server_default="queued"
    )
    source_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tenant_config_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tenant_config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_signals: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    attempt_count: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0"
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    side_effect_committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(100))


class IdempotencyRecord(TimestampedModel, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_idempotency_records_tenant_id_key"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["intake_cases.tenant_id", "intake_cases.id"],
            name="fk_idempotency_records_tenant_case",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["agent_jobs.tenant_id", "agent_jobs.id"],
            name="fk_idempotency_records_tenant_job",
        ),
        Index("ix_idempotency_records_tenant_id_id", "tenant_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    case_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    response: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class Approval(TimestampedModel, Base):
    __tablename__ = "approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["intake_cases.tenant_id", "intake_cases.id"],
            name="fk_approvals_tenant_case",
        ),
        Index("ix_approvals_tenant_id_id", "tenant_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    case_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="pending")
    decision: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
