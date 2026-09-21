"""Tenant-scoped tool ports with no database or event-loop dependency."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.agent import ToolData
from app.policy import TrustedSource
from app.tools.models import CreatedCase, CustomerSummary, UpdatedCase


class CustomerNotFoundError(LookupError):
    """A tenant-scoped customer identifier did not resolve to a record."""


class TenantToolPort(Protocol):
    def find_customer(
        self,
        tenant_id: UUID,
        *,
        email: str | None,
        external_id: str | None,
    ) -> CustomerSummary | None: ...

    def create_case(
        self,
        tenant_id: UUID,
        source: TrustedSource,
        *,
        customer_id: UUID | None,
        fields: Mapping[str, ToolData],
    ) -> CreatedCase: ...

    def update_case_fields(
        self,
        tenant_id: UUID,
        case_id: UUID,
        *,
        fields: Mapping[str, ToolData],
    ) -> UpdatedCase | None: ...

    def case_exists(self, tenant_id: UUID, case_id: UUID) -> bool: ...


@dataclass
class _CustomerRecord:
    tenant_id: UUID
    summary: CustomerSummary


@dataclass
class _CaseRecord:
    tenant_id: UUID
    id: UUID
    status: str
    channel: str
    subject: str
    body: str
    extracted_fields: dict[str, ToolData]


__all__ = ["CustomerNotFoundError", "TenantToolPort"]
