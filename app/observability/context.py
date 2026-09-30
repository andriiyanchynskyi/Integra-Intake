"""Explicit immutable correlation context for intake observations."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from uuid import UUID

from app.tenants.identifiers import SAFE_IDENTIFIER_PATTERN, SafeIdentifier


PROFILE_FINGERPRINT_PATTERN = r"^[0-9a-f]{64}$"


@dataclass(frozen=True, slots=True)
class ObservationContext:
    """Server-owned IDs carried explicitly across async and sync boundaries."""

    trace_id: UUID
    request_id: UUID | None = None
    tenant_id: UUID | None = None
    job_id: UUID | None = None
    case_id: UUID | None = None
    approval_id: UUID | None = None
    scenario_key: SafeIdentifier | None = None
    profile_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.trace_id, UUID):
            raise TypeError("trace_id must be a UUID")
        for name in (
            "request_id",
            "tenant_id",
            "job_id",
            "case_id",
            "approval_id",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, UUID):
                raise TypeError(f"{name} must be a UUID or None")
        if self.scenario_key is not None and re.fullmatch(
            SAFE_IDENTIFIER_PATTERN, self.scenario_key
        ) is None:
            raise TypeError("scenario_key must be a safe identifier or None")
        if self.profile_fingerprint is not None and re.fullmatch(
            PROFILE_FINGERPRINT_PATTERN, self.profile_fingerprint
        ) is None:
            raise TypeError("profile_fingerprint must be a 64-character SHA-256")

    def bind(self, **changes: object) -> ObservationContext:
        """Return a copy with explicitly named entity IDs bound."""

        allowed = {
            "trace_id",
            "request_id",
            "tenant_id",
            "job_id",
            "case_id",
            "approval_id",
            "scenario_key",
            "profile_fingerprint",
        }
        unknown = set(changes).difference(allowed)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise TypeError(f"unknown observation context field(s): {names}")
        if "trace_id" in changes and changes["trace_id"] is None:
            raise TypeError("trace_id cannot be None")
        return replace(self, **changes)


__all__ = ["ObservationContext"]
