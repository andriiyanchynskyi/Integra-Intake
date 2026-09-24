"""Typed trusted context and deterministic policy outcomes."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from uuid import UUID

from app.agent import AgentProposal, ToolData
from app.documents import NormalizedRateConfirmationDocument
from app.tenants.config import RoutingDecision, RoutingStatus, TenantConfig


def proposal_value_is_present(value: object) -> bool:
    """Return whether a proposal value can satisfy a required field."""

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return True


@dataclass(frozen=True, slots=True)
class RiskSignals:
    """Risk facts supplied by trusted runtime components."""

    safety_or_legal_risk: bool = False


@dataclass(frozen=True, slots=True)
class TrustedSource:
    """Inbound source data that the model cannot replace."""

    channel: str
    subject: str
    body: str
    document: NormalizedRateConfirmationDocument | None = None


def trusted_source_from_snapshot(value: object) -> TrustedSource:
    """Decode the legacy or document-aware trusted source snapshot."""

    if not isinstance(value, Mapping):
        raise RuntimeError("source snapshot is invalid")
    keys = set(value)
    if keys not in (
        {"channel", "subject", "body"},
        {"channel", "subject", "body", "document"},
    ):
        raise RuntimeError("source snapshot is invalid")
    if not all(isinstance(value[name], str) for name in ("channel", "subject", "body")):
        raise RuntimeError("source snapshot is invalid")
    document = None
    if "document" in value:
        try:
            document = NormalizedRateConfirmationDocument.model_validate(
                deepcopy(value["document"])
            )
        except Exception as error:
            raise RuntimeError("source snapshot is invalid") from error
    return TrustedSource(
        channel=value["channel"],
        subject=value["subject"],
        body=value["body"],
        document=document,
    )


@dataclass(frozen=True, slots=True)
class TrustedToolRuntimeContext:
    """Server-owned context held by a policy-gated executor."""

    tenant_id: UUID
    tenant_config: TenantConfig
    source: TrustedSource | None = None
    case_id: UUID | None = None
    risk_signals: RiskSignals = field(default_factory=RiskSignals)
    job_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PolicyInput:
    """Untrusted proposal plus trusted execution metadata."""

    proposal: AgentProposal
    requested_action: str
    requires_complete_fields: bool
    registered_actions: frozenset[str]
    runtime: TrustedToolRuntimeContext
    verified_present_fields: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class PolicyOutcome:
    """Stable decision data safe to append to an agent transcript."""

    decision: RoutingDecision
    status: RoutingStatus
    reason: str
    missing_required_fields: tuple[str, ...] = ()

    def as_tool_data(self) -> ToolData:
        return {
            "decision": self.decision.value,
            "status": self.status.value,
            "reason": self.reason,
            "missing_required_fields": list(self.missing_required_fields),
        }
