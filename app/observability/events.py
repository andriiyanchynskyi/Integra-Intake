"""Closed, source-free observation event contracts."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.agent.models import StopReason
from app.documents.models import DocumentExtractionError, DocumentMediaType
from app.tenants.config import RoutingDecision, RoutingStatus
from app.tenants.identifiers import SafeIdentifier


class EventName(str, Enum):
    HTTP_REQUEST_COMPLETED = "http.request.completed"
    AUTH_COMPLETED = "auth.completed"
    INBOUND_SIGNATURE_COMPLETED = "inbound.signature.completed"
    INTAKE_ENQUEUE_COMPLETED = "intake.enqueue.completed"
    DOCUMENT_NORMALIZATION_COMPLETED = "document.normalization.completed"
    PROFILE_RESOLUTION_COMPLETED = "profile.resolution.completed"
    RUNTIME_PREFLIGHT_COMPLETED = "runtime.preflight.completed"
    WORKER_JOB_CLAIMED = "worker.job.claimed"
    WORKER_JOB_RUN_STARTED = "worker.job.run_started"
    WORKER_JOB_RETRY_SCHEDULED = "worker.job.retry_scheduled"
    WORKER_JOB_LEASE_RECOVERED = "worker.job.lease_recovered"
    WORKER_JOB_FINISHED = "worker.job.finished"
    AGENT_STEP_COMPLETED = "agent.step.completed"
    AGENT_RUN_FINISHED = "agent.run.finished"
    PROVIDER_CALL_COMPLETED = "provider.call.completed"
    PROVIDER_STRUCTURED_OUTPUT_FAILED = "provider.structured_output.failed"
    POLICY_EVALUATED = "policy.evaluated"
    TOOL_EXECUTION_COMPLETED = "tool.execution.completed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    APPROVAL_EXPIRED = "approval.expired"
    APPROVAL_ACTION_COMPLETED = "approval.action_completed"
    PERSISTENCE_OPERATION_FAILED = "persistence.operation_failed"


class EventLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Component(str, Enum):
    API = "api"
    AUTH = "auth"
    INBOUND = "inbound"
    INTAKE = "intake"
    DOCUMENT = "document"
    PROFILE = "profile"
    RUNTIME = "runtime"
    WORKER = "worker"
    AGENT = "agent"
    PROVIDER = "provider"
    POLICY = "policy"
    TOOL = "tool"
    APPROVAL = "approval"
    PERSISTENCE = "persistence"


class OutcomeCode(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    AUTHENTICATED = "authenticated"
    REJECTED = "rejected"
    VERIFIED = "verified"
    MISSING = "missing"
    MALFORMED = "malformed"
    STALE = "stale"
    MISMATCH = "mismatch"
    CREATED = "created"
    REUSED = "reused"
    CONFLICT = "conflict"
    CLAIMED = "claimed"
    STARTED = "started"
    RETRY_SCHEDULED = "retry_scheduled"
    RECOVERED = "recovered"
    SUCCEEDED = "succeeded"
    FAILED_UNCERTAIN = "failed_uncertain"
    COMPLETED = "completed"
    TERMINAL = "terminal"
    TOOL_REQUESTED = "tool_requested"
    INVALID_SCHEMA = "invalid_schema"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    HTTP_ERROR = "http_error"
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"
    APPROVED = "approved"
    EXPIRED = "expired"
    EXECUTED = "executed"
    NOT_FOUND = "not_found"
    INVALID_ARGUMENTS = "invalid_arguments"
    BACKEND_FAILURE = "backend_failure"
    NO_RESULT = "no_result"


class RouteName(str, Enum):
    HEALTH = "/health"
    CASES = "/v1/cases"
    CASE = "/v1/cases/{case_id}"
    INTAKE = "/v1/intake"
    INBOUND_EMAIL = "/v1/inbound/email/webhook"
    APPROVAL_DECIDE = "/v1/approvals/{approval_id}/decide"
    UNKNOWN = "unknown"


class CredentialKind(str, Enum):
    SERVICE_API_KEY = "service_api_key"
    OPERATOR_API_KEY = "operator_api_key"
    INBOUND_WEBHOOK = "inbound_webhook"


class PreflightOutcome(str, Enum):
    CONTINUE = "continue"
    TERMINAL = "terminal"


class CapabilityErrorCode(str, Enum):
    UNAVAILABLE = "capability_unavailable"
    SNAPSHOT_INCOMPATIBLE = "snapshot_incompatible"


class PolicyReason(str, Enum):
    SAFETY_OR_LEGAL_RISK = "safety_or_legal_risk"
    UNKNOWN_INTAKE_TYPE = "unknown_intake_type"
    MISSING_REQUIRED_FIELDS = "missing_required_fields"
    ACTION_NOT_CONFIGURED = "action_not_configured"
    ACTION_NOT_ALLOWED = "action_not_allowed"
    APPROVAL_REQUIRED = "approval_required"
    ACTION_ALLOWED = "action_allowed"
    DOCUMENT_UNREADABLE = "document_unreadable"


class PersistenceOperation(str, Enum):
    ENQUEUE_JOB = "enqueue_job"
    CLAIM_JOB = "claim_job"
    RECOVER_LEASE = "recover_lease"
    SCHEDULE_RETRY = "schedule_retry"
    MARK_SUCCEEDED = "mark_succeeded"
    MARK_FAILED = "mark_failed"
    MARK_FAILED_UNCERTAIN = "mark_failed_uncertain"
    CREATE_APPROVAL = "create_approval"
    DECIDE_APPROVAL = "decide_approval"


class WorkerErrorCode(str, Enum):
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RETRY_EXHAUSTED = "retry_exhausted"
    SIDE_EFFECT_COMMITTED = "side_effect_committed"
    UNEXPECTED_AFTER_SIDE_EFFECT = "unexpected_after_side_effect"
    WORKER_ERROR = "worker_error"
    AGENT_FAILED = "agent_failed"
    AGENT_FAILED_AFTER_SIDE_EFFECT = "agent_failed_after_side_effect"
    LEASE_EXPIRED = "lease_expired"
    LEASE_EXPIRED_AFTER_SIDE_EFFECT = "lease_expired_after_side_effect"
    PERSISTENCE_ERROR = "persistence_error"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    SNAPSHOT_INCOMPATIBLE = "snapshot_incompatible"


SafeFieldName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,99}$"),
]
ClientCorrelationId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,128}$"),
]
ProfileFingerprint = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]


class ObservationEvent(BaseModel):
    """The only payload shape accepted by an observation sink."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event: EventName
    event_version: Literal[1] = 1
    level: EventLevel = EventLevel.INFO
    trace_id: UUID
    request_id: UUID | None = None
    tenant_id: UUID | None = None
    job_id: UUID | None = None
    case_id: UUID | None = None
    approval_id: UUID | None = None
    component: Component
    outcome: OutcomeCode | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    queue_latency_ms: Annotated[int, Field(ge=0)] | None = None
    total_latency_ms: Annotated[int, Field(ge=0)] | None = None
    route: RouteName | None = None
    method: Literal["GET", "POST"] | None = None
    status_code: Annotated[int, Field(ge=100, le=599)] | None = None
    request_bytes: Annotated[int, Field(ge=0)] | None = None
    attempt_count: Annotated[int, Field(ge=0)] | None = None
    retry_delay_ms: Annotated[int, Field(ge=0)] | None = None
    step: Annotated[int, Field(ge=1, le=8)] | None = None
    steps: Annotated[int, Field(ge=0, le=8)] | None = None
    stop_reason: StopReason | None = None
    policy_decision: RoutingDecision | None = None
    routing_status: RoutingStatus | None = None
    policy_reason: PolicyReason | None = None
    missing_fields: tuple[SafeFieldName, ...] = ()
    document_media_type: DocumentMediaType | None = None
    document_error: DocumentExtractionError | None = None
    document_bytes: Annotated[int, Field(ge=0)] | None = None
    document_pages: Annotated[int, Field(ge=0)] | None = None
    document_text_chars: Annotated[int, Field(ge=0)] | None = None
    provider_call_index: Annotated[int, Field(ge=1, le=2)] | None = None
    validation_retry: bool | None = None
    prompt_tokens: Annotated[int, Field(ge=0)] | None = None
    completion_tokens: Annotated[int, Field(ge=0)] | None = None
    total_tokens: Annotated[int, Field(ge=0)] | None = None
    result_is_none: bool | None = None
    side_effect_committed: bool | None = None
    credential_kind: CredentialKind | None = None
    client_correlation_id: ClientCorrelationId | None = None
    client_correlation_invalid: bool | None = None
    persistence_operation: PersistenceOperation | None = None
    worker_error_code: WorkerErrorCode | None = None
    scenario_key: SafeIdentifier | None = None
    profile_fingerprint: ProfileFingerprint | None = None
    document_kind: SafeIdentifier | None = None
    normalizer_key: SafeIdentifier | None = None
    normalizer_version: Annotated[int, Field(ge=1)] | None = None
    intake_type: SafeIdentifier | None = None
    intake_type_known: bool | None = None
    action_key: SafeIdentifier | None = None
    action_known: bool | None = None
    preflight_outcome: PreflightOutcome | None = None
    capability_error: CapabilityErrorCode | None = None

    @field_validator("missing_fields")
    @classmethod
    def normalize_missing_fields(
        cls, value: tuple[SafeFieldName, ...]
    ) -> tuple[SafeFieldName, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def validate_correlations_and_usage(self) -> ObservationEvent:
        if self.client_correlation_invalid and self.client_correlation_id is not None:
            raise ValueError(
                "client_correlation_id cannot be present when its value is invalid"
            )
        usage = (
            self.prompt_tokens,
            self.completion_tokens,
            self.total_tokens,
        )
        if all(value is not None for value in usage):
            prompt_tokens, completion_tokens, total_tokens = usage
            assert prompt_tokens is not None
            assert completion_tokens is not None
            assert total_tokens is not None
            if total_tokens != prompt_tokens + completion_tokens:
                raise ValueError(
                    "total_tokens must equal prompt_tokens plus completion_tokens"
                )
        if self.intake_type_known is not None and (
            self.intake_type_known != (self.intake_type is not None)
        ):
            raise ValueError("intake_type_known must match intake_type presence")
        if self.action_known is not None and (
            self.action_known != (self.action_key is not None)
        ):
            raise ValueError("action_known must match action_key presence")
        if (self.normalizer_key is None) != (self.normalizer_version is None):
            raise ValueError(
                "normalizer_key and normalizer_version must be supplied together"
            )
        return self


__all__ = [
    "ClientCorrelationId",
    "CapabilityErrorCode",
    "Component",
    "CredentialKind",
    "EventLevel",
    "EventName",
    "ObservationEvent",
    "OutcomeCode",
    "PersistenceOperation",
    "PolicyReason",
    "PreflightOutcome",
    "ProfileFingerprint",
    "RouteName",
    "SafeFieldName",
    "WorkerErrorCode",
]
