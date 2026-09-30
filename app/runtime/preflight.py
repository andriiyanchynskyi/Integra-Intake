"""Deterministic, provider-free checks for trusted document snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from app.documents import DocumentExtractionError, NormalizedDocument
from app.policy.models import TrustedSource
from app.tenants.config import RoutingStatus, TenantConfig

if TYPE_CHECKING:
    from app.tenants.compiled import CompiledTenantProfile, ImmutableTenantConfig


@dataclass(frozen=True, slots=True)
class ContinuePreflight:
    """The trusted source is eligible for the normal agent runtime."""

    outcome: Literal["continue"] = "continue"


@dataclass(frozen=True, slots=True)
class TerminalPreflightResult:
    """A safe, deterministic result that must not enter the agent loop."""

    routing_status: RoutingStatus
    reason: Literal[
        "document_unreadable",
        "capability_unavailable",
        "snapshot_incompatible",
    ]
    target_intake_type: str | None
    missing_required_fields: tuple[str, ...]
    document_kind: str | None
    extraction_error: DocumentExtractionError | None


PreflightResult = ContinuePreflight | TerminalPreflightResult


def preflight_document(
    source: TrustedSource,
    config: TenantConfig | ImmutableTenantConfig,
    compiled_profile: CompiledTenantProfile | None = None,
) -> PreflightResult:
    """Return a closed result using only trusted source/profile data."""

    document = source.document
    if document is None:
        return ContinuePreflight()

    target_intake_type = document.target_intake_type
    intake = next(
        (item for item in config.intake_types if item.name == target_intake_type),
        None,
    )
    missing_required_fields = (
        tuple(sorted(intake.required_fields)) if intake is not None else ()
    )

    if compiled_profile is not None:
        binding = compiled_profile.documents.get(document.document_kind)
        if (
            binding is None
            or binding.target_intake_type != document.target_intake_type
            or binding.normalizer_key != document.normalizer_key
            or binding.normalizer_version != document.normalizer_version
            or document.media_type not in binding.capability.supported_media_types
        ):
            return TerminalPreflightResult(
                routing_status=RoutingStatus.REJECTED,
                reason="snapshot_incompatible",
                target_intake_type=target_intake_type,
                missing_required_fields=missing_required_fields,
                document_kind=document.document_kind,
                extraction_error=None,
            )

    if intake is None:
        return TerminalPreflightResult(
            routing_status=RoutingStatus.REJECTED,
            reason="capability_unavailable",
            target_intake_type=target_intake_type,
            missing_required_fields=(),
            document_kind=document.document_kind,
            extraction_error=None,
        )

    if document.extraction_error is not None:
        return TerminalPreflightResult(
            routing_status=RoutingStatus.AWAITING_INPUT,
            reason="document_unreadable",
            target_intake_type=target_intake_type,
            missing_required_fields=missing_required_fields,
            document_kind=document.document_kind,
            extraction_error=document.extraction_error,
        )

    return ContinuePreflight()


def document_details(
    source: TrustedSource,
) -> tuple[str | None, str | None, DocumentExtractionError | None]:
    """Return safe document identity fields for a capability terminal event."""

    document: NormalizedDocument | None = source.document
    if document is None:
        return None, None, None
    return document.document_kind, document.target_intake_type, document.extraction_error


__all__ = [
    "ContinuePreflight",
    "PreflightResult",
    "TerminalPreflightResult",
    "document_details",
    "preflight_document",
]
