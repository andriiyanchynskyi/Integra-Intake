"""Strict, privacy-safe contracts for the deterministic freight corpus."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.agent import AgentProposal, ProposalPriority


DATASET_VERSION = 1
RUNNER_VERSION = "freight_eval.v1"

CORE_COUNTS = {
    "body_only": 16,
    "document": 12,
    "signed_webhook": 6,
    "multi_request": 4,
    "tenant_idempotency": 2,
}
ADVERSARIAL_COUNTS = {
    "body_override": 4,
    "document_override": 3,
    "webhook_override": 3,
}


class EvalCaseKind(str, Enum):
    AGENT = "agent"
    WEBHOOK_IDEMPOTENCY = "webhook_idempotency"
    TENANT_ISOLATION = "tenant_isolation"


class EvalSourceKind(str, Enum):
    BODY = "body"
    DOCUMENT = "document"
    INBOUND_WEBHOOK = "inbound_webhook"


class ClaimLevel(str, Enum):
    DOWNSTREAM_FROM_TYPED_PROPOSAL = "downstream_from_typed_proposal"
    DETERMINISTIC_SERVER_INVARIANT = "deterministic_server_invariant"


class ReportCategory(str, Enum):
    BODY_ONLY = "body_only"
    DOCUMENT = "document"
    SIGNED_WEBHOOK = "signed_webhook"
    MULTI_REQUEST = "multi_request"
    TENANT_IDEMPOTENCY = "tenant_idempotency"
    BODY_OVERRIDE = "body_override"
    DOCUMENT_OVERRIDE = "document_override"
    WEBHOOK_OVERRIDE = "webhook_override"


class ReportMismatchCode(str, Enum):
    APPROVAL_MISMATCH = "approval_mismatch"
    CASE_SIDE_EFFECT_MISMATCH = "case_side_effect_mismatch"
    DOCUMENT_EXTRACTION_ERROR_MISMATCH = "document_extraction_error_mismatch"
    DOCUMENT_FIXTURE_UNAVAILABLE = "document_fixture_unavailable"
    DUPLICATE_REUSE_MISMATCH = "duplicate_reuse_mismatch"
    IDEMPOTENCY_CONFLICT_MISMATCH = "idempotency_conflict_mismatch"
    INTAKE_TYPE_MISMATCH = "intake_type_mismatch"
    INVALID_INVARIANT_SOURCE = "invalid_invariant_source"
    LLM_CALLS_MISMATCH = "llm_calls_mismatch"
    MISSING_FIELDS_MISMATCH = "missing_fields_mismatch"
    POLICY_DECISION_MISMATCH = "policy_decision_mismatch"
    PROVIDER_CALLED = "provider_called"
    PROVIDER_CALLS_MISMATCH = "provider_calls_mismatch"
    ROUTING_REASON_MISMATCH = "routing_reason_mismatch"
    ROUTING_STATUS_MISMATCH = "routing_status_mismatch"
    SCRIPTED_PROPOSALS_EXHAUSTED = "scripted_proposals_exhausted"
    SCRIPTED_PROPOSALS_UNUSED = "scripted_proposals_unused"
    SIGNATURE_VERIFICATION_MISMATCH = "signature_verification_mismatch"
    SOURCE_EXECUTION_FAILED = "source_execution_failed"
    STEPS_MISMATCH = "steps_mismatch"
    STOP_REASON_MISMATCH = "stop_reason_mismatch"
    TENANT_ISOLATION_MISMATCH = "tenant_isolation_mismatch"
    TOOL_NAME_MISMATCH = "tool_name_mismatch"
    UNREADABLE_FALLBACK_MISMATCH = "unreadable_fallback_mismatch"
    WEBHOOK_SOURCE_UNAVAILABLE = "webhook_source_unavailable"


REPORT_CATEGORY_VALUES = frozenset(item.value for item in ReportCategory)
REPORT_MISMATCH_VALUES = frozenset(item.value for item in ReportMismatchCode)


class _EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _as_tuple(value: object, field_name: str) -> tuple[object, ...]:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    raise TypeError(f"{field_name} must be a list")


class FreightBodySource(_EvalModel):
    kind: Literal[EvalSourceKind.BODY]
    channel: StrictStr = Field(min_length=1, max_length=100)
    subject: StrictStr = Field(min_length=1, max_length=500)
    body: StrictStr = Field(min_length=1, max_length=100_000)


class FreightDocumentSource(_EvalModel):
    kind: Literal[EvalSourceKind.DOCUMENT]
    fixture_id: StrictStr = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    channel: StrictStr = "eval_document"
    subject: StrictStr = Field(min_length=1, max_length=500)
    body: StrictStr = "Rate confirmation document received."


class FreightWebhookSource(_EvalModel):
    kind: Literal[EvalSourceKind.INBOUND_WEBHOOK]
    provider_id: StrictStr = Field(min_length=1, max_length=200)
    from_addr: StrictStr = Field(min_length=1, max_length=320)
    subject: StrictStr = Field(max_length=500)
    body: StrictStr = Field(max_length=100_000)
    document_fixture_id: StrictStr | None = Field(default=None)
    second_body: StrictStr | None = Field(default=None, max_length=100_000)
    second_tenant: StrictBool = False


FreightEvalSource = Annotated[
    FreightBodySource | FreightDocumentSource | FreightWebhookSource,
    Field(discriminator="kind"),
]


class FreightEvalMetadata(_EvalModel):
    adversarial: StrictBool
    claim_level: ClaimLevel

    @field_validator("claim_level", mode="before")
    @classmethod
    def parse_claim_level(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return ClaimLevel(value)
            except ValueError:
                return value
        return value


class _FreightObservationFields(_EvalModel):
    intake_type: StrictStr | None = None
    missing_required_fields: tuple[StrictStr, ...] | None = None
    policy_decision: StrictStr | None = None
    routing_status: StrictStr | None = None
    routing_reason: StrictStr | None = None
    tool_name: StrictStr | None = None
    approval_required: StrictBool | None = None
    case_created: StrictBool | None = None
    duplicate_reused: StrictBool | None = None
    conflict_raised: StrictBool | None = None
    tenant_isolated: StrictBool | None = None
    signature_verified: StrictBool | None = None
    document_extraction_error: StrictStr | None = None
    provider_calls: StrictInt | None = None
    llm_calls: StrictInt | None = None
    steps: StrictInt | None = None
    stop_reason: StrictStr | None = None

    @field_validator("missing_required_fields", mode="before")
    @classmethod
    def normalize_missing_fields(cls, value: object) -> tuple[object, ...] | None:
        if value is None:
            return None
        return _as_tuple(value, "missing_required_fields")

    @field_validator("missing_required_fields")
    @classmethod
    def sort_missing_fields(cls, value: tuple[StrictStr, ...] | None) -> tuple[StrictStr, ...] | None:
        if value is None:
            return None
        if len(value) != len(set(value)):
            raise ValueError("missing_required_fields must be unique")
        return tuple(sorted(value))

    @field_validator("provider_calls", "llm_calls")
    @classmethod
    def non_negative_calls(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("call count must be non-negative")
        return value


class FreightEvalObservation(_FreightObservationFields):
    """Safe structured observations produced by the runner."""


class FreightEvalExpected(_FreightObservationFields):
    """Safe expected observations authored in the versioned dataset."""


class FreightEvalCase(_EvalModel):
    id: StrictStr = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    category: StrictStr = Field(min_length=1)
    language: Literal["en", "uk", "mixed", "none"]
    kind: EvalCaseKind
    source: FreightEvalSource
    scripted_proposals: tuple[AgentProposal, ...] = ()
    expected: FreightEvalExpected
    metadata: FreightEvalMetadata

    @field_validator("kind", mode="before")
    @classmethod
    def parse_kind(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return EvalCaseKind(value)
            except ValueError:
                return value
        return value

    @field_validator("source", mode="before")
    @classmethod
    def parse_source_kind(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        kind = result.get("kind")
        if isinstance(kind, str):
            try:
                result["kind"] = EvalSourceKind(kind)
            except ValueError:
                pass
        return result

    @field_validator("scripted_proposals", mode="before")
    @classmethod
    def normalize_scripted_proposals(cls, value: object) -> tuple[object, ...]:
        proposals = _as_tuple(value, "scripted_proposals")
        normalized: list[object] = []
        for proposal in proposals:
            if not isinstance(proposal, dict):
                normalized.append(proposal)
                continue
            item = dict(proposal)
            priority = item.get("priority")
            if isinstance(priority, str):
                try:
                    item["priority"] = ProposalPriority(priority)
                except ValueError:
                    pass
            normalized.append(item)
        return tuple(normalized)

    @model_validator(mode="after")
    def validate_kind_contract(self) -> FreightEvalCase:
        semantic = self.kind is EvalCaseKind.AGENT
        if semantic:
            if not self.scripted_proposals:
                raise ValueError("agent cases require scripted_proposals")
            if self.metadata.claim_level is not ClaimLevel.DOWNSTREAM_FROM_TYPED_PROPOSAL:
                raise ValueError("agent cases require downstream claim level")
            required = (
                "intake_type",
                "missing_required_fields",
                "policy_decision",
                "routing_status",
                "routing_reason",
                "tool_name",
                "approval_required",
                "case_created",
            )
            if any(getattr(self.expected, name) is None for name in required):
                raise ValueError("agent expected outcome is incomplete")
        else:
            if self.scripted_proposals:
                raise ValueError("server-invariant cases forbid scripted_proposals")
            if self.metadata.claim_level is not ClaimLevel.DETERMINISTIC_SERVER_INVARIANT:
                raise ValueError("server-invariant cases require invariant claim level")
            if self.kind is EvalCaseKind.WEBHOOK_IDEMPOTENCY:
                if self.expected.duplicate_reused is None or self.expected.conflict_raised is None:
                    raise ValueError("idempotency expected outcome is incomplete")
            elif self.expected.tenant_isolated is None:
                raise ValueError("tenant-isolation expected outcome is incomplete")
        return self


class FreightEvalCaseResult(_EvalModel):
    id: StrictStr
    category: StrictStr
    passed: StrictBool
    observation: FreightEvalObservation
    expected: FreightEvalExpected
    mismatches: tuple[StrictStr, ...] = ()

    @field_validator("mismatches", mode="before")
    @classmethod
    def normalize_mismatches(cls, value: object) -> tuple[object, ...]:
        return _as_tuple(value, "mismatches")

    @field_validator("mismatches")
    @classmethod
    def sort_mismatches(cls, value: tuple[StrictStr, ...]) -> tuple[StrictStr, ...]:
        if len(value) != len(set(value)):
            raise ValueError("mismatches must be unique")
        return tuple(sorted(value))

    def safe_failure_message(self) -> str:
        if self.passed:
            return f"{self.id}: passed"
        mismatch_text = ", ".join(self.mismatches) or "unspecified_mismatch"
        return f"{self.id}: {mismatch_text}"


class FreightEvalReportCase(_EvalModel):
    id: StrictStr = Field(
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        max_length=120,
    )
    category: ReportCategory
    passed: StrictBool
    mismatches: tuple[ReportMismatchCode, ...] = ()

    @field_validator("mismatches", mode="before")
    @classmethod
    def normalize_report_mismatches(cls, value: object) -> tuple[object, ...]:
        return _as_tuple(value, "mismatches")

    @field_validator("mismatches")
    @classmethod
    def sort_report_mismatches(
        cls, value: tuple[ReportMismatchCode, ...]
    ) -> tuple[ReportMismatchCode, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))


class FreightEvalReport(_EvalModel):
    dataset_version: Literal[1]
    runner_version: Literal["freight_eval.v1"]
    commit_sha: StrictStr | None
    total: StrictInt
    passed: StrictInt
    failed: StrictInt
    pass_rate: StrictFloat
    categories: dict[ReportCategory, StrictInt]
    cases: tuple[FreightEvalReportCase, ...]

    @field_validator("total", "passed", "failed")
    @classmethod
    def non_negative_totals(cls, value: int) -> int:
        if value < 0:
            raise ValueError("report counts must be non-negative")
        return value

    @model_validator(mode="after")
    def validate_report_counts(self) -> FreightEvalReport:
        if self.total <= 0:
            raise ValueError("report must contain at least one case")
        if self.passed + self.failed != self.total:
            raise ValueError("report counts do not add up")
        if len(self.cases) != self.total:
            raise ValueError("report case count does not match total")
        if self.pass_rate != self.passed / self.total:
            raise ValueError("report pass rate does not match counts")
        return self


class FreightEvalDataset(_EvalModel):
    version: Literal[1]
    cases: tuple[FreightEvalCase, ...]

    @field_validator("cases", mode="before")
    @classmethod
    def normalize_cases(cls, value: object) -> tuple[object, ...]:
        return _as_tuple(value, "cases")


__all__ = [
    "ADVERSARIAL_COUNTS",
    "CORE_COUNTS",
    "DATASET_VERSION",
    "RUNNER_VERSION",
    "ClaimLevel",
    "EvalCaseKind",
    "EvalSourceKind",
    "REPORT_CATEGORY_VALUES",
    "REPORT_MISMATCH_VALUES",
    "ReportCategory",
    "ReportMismatchCode",
    "FreightBodySource",
    "FreightDocumentSource",
    "FreightEvalCase",
    "FreightEvalCaseResult",
    "FreightEvalDataset",
    "FreightEvalExpected",
    "FreightEvalMetadata",
    "FreightEvalObservation",
    "FreightEvalReport",
    "FreightEvalReportCase",
    "FreightEvalSource",
    "FreightWebhookSource",
]
