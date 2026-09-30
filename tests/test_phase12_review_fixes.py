"""Focused regression contracts for the Phase 12 review findings."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.agent import AgentProposal, AgentRunResult, ProposalPriority, RunStatus, StopReason
from app.documents import (
    DocumentExtractionError,
    DocumentInput,
    DocumentMediaType,
    NormalizedDocument,
)
from app.documents.registry import DocumentNormalizerCapability, DocumentNormalizerRegistry
from app.domain.intake import (
    EnqueueDocumentIntakeCommand,
    EnqueueIntakeCommand,
    IdempotencyConflict,
    IntakeEnqueueService,
    canonical_document_intake_hash,
)
from app.domain.job_repository import ClaimedJob
from app.observability import (
    CapabilityErrorCode,
    EventName,
    ObservationContext,
    OutcomeCode,
    PersistenceOperation,
    RecordingObserver,
)
from app.policy import (
    PolicyEngine,
    PolicyInput,
    RiskSignals,
    TrustedSource,
    TrustedToolRuntimeContext,
)
from app.runtime.factory import AgentRuntimeFactory
from app.runtime.gateway import WorkerAsyncGateway
from app.runtime.preflight import TerminalPreflightResult, preflight_document
from app.runtime.profiles import (
    ResolvedTenantProfile,
    TenantProfileResolver,
    canonical_json_bytes,
)
from app.tenants.compiled import compile_tenant_profile
from app.tenants.config import RoutingDecision, RoutingStatus, TenantConfig
from app.tenants.loader import load_tenant_config
from app.workers import agent_worker as agent_worker_module
from app.workers.agent_worker import AgentWorker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = PROJECT_ROOT / "examples"
TENANT_ID = UUID("00000000-0000-0000-0000-000000000901")


def _profile() -> ResolvedTenantProfile:
    return TenantProfileResolver(EXAMPLES).resolve("freight-broker")


def _proposal(*, injection: bool = False) -> AgentProposal:
    return AgentProposal.model_validate(
        {
            "intake_type": "rate_confirmation",
            "fields": [],
            "missing_required_fields": [],
            "priority": ProposalPriority.NORMAL,
            "contains_injection_or_override_attempt": injection,
            "rationale_short": "Synthetic regression proposal.",
            "tool_calls": [],
            "confidence": 1.0,
        }
    )


def _normalized_document(
    *,
    document_kind: str = "rate_confirmation",
    target_intake_type: str = "rate_confirmation",
    normalizer_key: str = "bounded_text_pdf",
    normalizer_version: int = 1,
    media_type: DocumentMediaType = DocumentMediaType.TEXT,
    text: str | None = "Synthetic normalized document.",
    extraction_error: DocumentExtractionError | None = None,
    sha256: str = "a" * 64,
) -> NormalizedDocument:
    return NormalizedDocument(
        document_kind=document_kind,
        target_intake_type=target_intake_type,
        media_type=media_type,
        sha256=sha256,
        normalizer_key=normalizer_key,
        normalizer_version=normalizer_version,
        text=text if extraction_error is None else None,
        extraction_error=extraction_error,
    )


def _document_input(
    *,
    body: str = "Synthetic envelope.",
    text: str | None = "Origin: synthetic",
    content: bytes | None = None,
    media_type: DocumentMediaType = DocumentMediaType.TEXT,
) -> DocumentInput:
    payload: dict[str, object] = {
        "channel": "email",
        "subject": "Synthetic document",
        "body": body,
        "media_type": media_type,
    }
    if text is not None:
        payload["text"] = text
    if content is not None:
        payload["content"] = content
    return DocumentInput.model_validate(payload)


def test_unreadable_document_is_denied_even_with_trusted_risk_signal() -> None:
    profile = _profile().config
    runtime = TrustedToolRuntimeContext(
        tenant_id=TENANT_ID,
        tenant_config=profile,
        source=TrustedSource(
            channel="email",
            subject="Synthetic document",
            body="Synthetic envelope.",
            document=_normalized_document(
                media_type=DocumentMediaType.PDF,
                text=None,
                extraction_error=DocumentExtractionError.PDF_MALFORMED,
            ),
        ),
        risk_signals=RiskSignals(safety_or_legal_risk=True),
    )

    outcome = PolicyEngine().evaluate(
        PolicyInput(
            proposal=_proposal(),
            requested_action="create_case",
            requires_complete_fields=True,
            registered_actions=frozenset({"create_case"}),
            runtime=runtime,
        )
    )

    assert outcome.decision is RoutingDecision.DENY
    assert outcome.status is RoutingStatus.AWAITING_INPUT
    assert outcome.reason == "document_unreadable"


def test_unreadable_document_is_denied_even_with_proposal_injection_flag() -> None:
    profile = _profile().config
    runtime = TrustedToolRuntimeContext(
        tenant_id=TENANT_ID,
        tenant_config=profile,
        source=TrustedSource(
            channel="email",
            subject="Synthetic document",
            body="Synthetic envelope.",
            document=_normalized_document(
                media_type=DocumentMediaType.PDF,
                text=None,
                extraction_error=DocumentExtractionError.PDF_MALFORMED,
            ),
        ),
    )

    outcome = PolicyEngine().evaluate(
        PolicyInput(
            proposal=_proposal(injection=True),
            requested_action="create_case",
            requires_complete_fields=True,
            registered_actions=frozenset({"create_case"}),
            runtime=runtime,
        )
    )

    assert outcome.decision is RoutingDecision.DENY
    assert outcome.status is RoutingStatus.AWAITING_INPUT
    assert outcome.reason == "document_unreadable"


@pytest.mark.parametrize(
    ("first_document", "second_document"),
    (
        (
            _document_input(body="Envelope A"),
            _document_input(body="Envelope B"),
        ),
        (
            _document_input(
                body="Envelope",
                text=None,
                content=b"payload-a",
                media_type=DocumentMediaType.PDF,
            ),
            _document_input(
                body="Envelope",
                text=None,
                content=b"payload-b",
                media_type=DocumentMediaType.PDF,
            ),
        ),
    ),
)
def test_document_hash_includes_original_envelope_body_and_content(
    first_document: DocumentInput,
    second_document: DocumentInput,
) -> None:
    first = EnqueueDocumentIntakeCommand(
        tenant_id=TENANT_ID,
        tenant_slug="freight-broker",
        document=first_document,
        idempotency_key="document-hash-regression",
    )
    second = EnqueueDocumentIntakeCommand(
        tenant_id=TENANT_ID,
        tenant_slug="freight-broker",
        document=second_document,
        idempotency_key="document-hash-regression",
    )

    assert canonical_document_intake_hash(first) != canonical_document_intake_hash(
        second
    )


def _profile_with_document_kind(document_kind: str) -> ResolvedTenantProfile:
    config = load_tenant_config(EXAMPLES / "freight-broker.yaml")
    payload = deepcopy(config.model_dump(mode="json"))
    documents = payload["documents"]
    assert isinstance(documents, dict)
    binding = documents.pop("rate_confirmation")
    assert isinstance(binding, dict)
    documents[document_kind] = binding
    changed = TenantConfig.model_validate(payload)
    snapshot = changed.model_dump(mode="json")
    fingerprint = hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()
    return ResolvedTenantProfile(
        config=changed,
        snapshot=snapshot,
        sha256=fingerprint,
        compiled=compile_tenant_profile(changed, profile_fingerprint=fingerprint),
    )


@dataclass
class _CountingNormalizer:
    calls: int = 0

    def normalize(self, value: DocumentInput, **kwargs: object) -> NormalizedDocument:
        self.calls += 1
        return _normalized_document(
            document_kind=str(kwargs["document_kind"]),
            target_intake_type=str(kwargs["target_intake_type"]),
            normalizer_key=str(kwargs["normalizer_key"]),
            normalizer_version=int(kwargs["normalizer_version"]),
            media_type=value.media_type,
        )


class _MemoryTransaction(AbstractAsyncContextManager["_MemoryTransaction"]):
    async def __aenter__(self) -> "_MemoryTransaction":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _MemorySession:
    def begin(self) -> _MemoryTransaction:
        return _MemoryTransaction()

    def begin_nested(self) -> _MemoryTransaction:
        return _MemoryTransaction()

    async def flush(self) -> None:
        return None


class _StaticProfileResolver:
    def __init__(self, profile: ResolvedTenantProfile) -> None:
        self.profile = profile

    def resolve(self, tenant_slug: str) -> ResolvedTenantProfile:
        assert tenant_slug == self.profile.config.slug
        return self.profile


class _MemoryRepository:
    def __init__(self, profile: ResolvedTenantProfile) -> None:
        self.profile = profile
        self.record: SimpleNamespace | None = None
        self.job_id: UUID | None = None
        self.trace_id: UUID | None = None

    async def get_idempotency_for_tenant(
        self, tenant_id: UUID, key: str, *, for_update: bool
    ) -> SimpleNamespace | None:
        del tenant_id, key, for_update
        return self.record

    async def create_job(
        self, command: EnqueueIntakeCommand, profile: ResolvedTenantProfile
    ) -> SimpleNamespace:
        del profile
        self.job_id = uuid4()
        self.trace_id = command.trace_id
        return SimpleNamespace(id=self.job_id)

    async def create_idempotency_record(
        self, tenant_id: UUID, key: str, request_hash: str, job_id: UUID
    ) -> None:
        self.record = SimpleNamespace(
            tenant_id=tenant_id,
            key=key,
            request_hash=request_hash,
            job_id=job_id,
        )

    async def get_observation_identity(
        self, job_id: UUID, *, tenant_id: UUID
    ) -> SimpleNamespace | None:
        if job_id != self.job_id or tenant_id != TENANT_ID:
            return None
        assert self.trace_id is not None
        return SimpleNamespace(
            trace_id=self.trace_id,
            tenant_config_snapshot=self.profile.snapshot,
            tenant_config_sha256=self.profile.sha256,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_document", "second_document"),
    (
        (
            _document_input(body="Envelope A"),
            _document_input(body="Envelope B"),
        ),
        (
            _document_input(
                body="Envelope",
                text=None,
                content=b"payload-a",
                media_type=DocumentMediaType.PDF,
            ),
            _document_input(
                body="Envelope",
                text=None,
                content=b"payload-b",
                media_type=DocumentMediaType.PDF,
            ),
        ),
    ),
)
async def test_document_identity_uses_original_envelope_and_skips_duplicate_normalization(
    first_document: DocumentInput,
    second_document: DocumentInput,
) -> None:
    profile = _profile()
    normalizer = _CountingNormalizer()
    service = IntakeEnqueueService(
        _MemorySession(),
        _StaticProfileResolver(profile),
        document_normalizer=normalizer,
    )
    repository = _MemoryRepository(profile)
    service.repository = repository  # type: ignore[assignment]
    tenant_id = TENANT_ID

    first = await service.enqueue_document(
        EnqueueDocumentIntakeCommand(
            tenant_id=tenant_id,
            tenant_slug="freight-broker",
            document=first_document,
            idempotency_key="document-envelope-regression",
        )
    )

    with pytest.raises(IdempotencyConflict):
        await service.enqueue_document(
            EnqueueDocumentIntakeCommand(
                tenant_id=tenant_id,
                tenant_slug="freight-broker",
                document=second_document,
                idempotency_key="document-envelope-regression",
            )
        )

    assert first.created is True
    assert normalizer.calls == 1


@pytest.mark.asyncio
async def test_document_duplicate_ignores_changed_profile_binding_and_skips_normalizer() -> None:
    original_profile = _profile()
    changed_profile = _profile_with_document_kind("future_note")
    resolver = _StaticProfileResolver(original_profile)
    normalizer = _CountingNormalizer()
    service = IntakeEnqueueService(
        _MemorySession(),
        resolver,
        document_normalizer=normalizer,
    )
    repository = _MemoryRepository(original_profile)
    service.repository = repository  # type: ignore[assignment]
    document = _document_input(body="Stable envelope", text="Stable payload")
    command = EnqueueDocumentIntakeCommand(
        tenant_id=TENANT_ID,
        tenant_slug="freight-broker",
        document=document,
        idempotency_key="document-binding-regression",
    )

    first = await service.enqueue_document(command)
    resolver.profile = changed_profile
    second = await service.enqueue_document(command)

    assert first.created is True
    assert second.created is False
    assert second.job_id == first.job_id
    assert normalizer.calls == 1


def test_document_registry_dispatches_a_bound_normalizer_factory() -> None:
    marker = SimpleNamespace(normalize=lambda *args, **kwargs: None)
    calls: list[tuple[object, object]] = []

    def factory(*, observer: object, context: object) -> object:
        calls.append((observer, context))
        return marker

    capability = DocumentNormalizerCapability(
        key="fixture_normalizer",
        version=1,
        supported_media_types=frozenset({DocumentMediaType.TEXT}),
        normalizer_factory=factory,
    )
    registry = DocumentNormalizerRegistry((capability,))
    observer = object()
    context = object()

    normalizer = registry.require("fixture_normalizer", 1).create(
        observer=observer,
        context=context,
    )

    assert normalizer is marker
    assert calls == [(observer, context)]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("document_kind", "future_note"),
        ("target_intake_type", "future_request"),
        ("normalizer_key", "future_text"),
        ("normalizer_version", 9),
    ),
)
def test_preflight_rejects_persisted_document_binding_mismatch(
    field: str, value: object
) -> None:
    profile = _profile()
    compiled = profile.compiled
    assert compiled is not None
    binding = compiled.documents["rate_confirmation"]
    document_values: dict[str, object] = {
        "document_kind": binding.document_kind,
        "target_intake_type": binding.target_intake_type,
        "normalizer_key": binding.normalizer_key,
        "normalizer_version": binding.normalizer_version,
    }
    document_values[field] = value
    document = _normalized_document(**document_values)  # type: ignore[arg-type]

    result = preflight_document(
        TrustedSource(
            channel="email",
            subject="Synthetic document",
            body="Synthetic envelope.",
            document=document,
        ),
        profile.config,
        compiled_profile=compiled,
    )

    assert isinstance(result, TerminalPreflightResult)
    assert result.reason == "snapshot_incompatible"


def test_runtime_maps_mismatched_profile_snapshot_to_stable_terminal_result() -> None:
    profile = _profile()
    tampered_snapshot = dict(profile.snapshot)
    tampered_snapshot["display_name"] = "Synthetic changed profile"
    claimed_job = SimpleNamespace(
        id=uuid4(),
        tenant_id=TENANT_ID,
        trace_id=uuid4(),
        source_snapshot={
            "channel": "email",
            "subject": "Synthetic request",
            "body": "Synthetic envelope.",
        },
        tenant_config_snapshot=tampered_snapshot,
        tenant_config_sha256=profile.sha256,
        risk_signals={"safety_or_legal_risk": False},
    )
    observer = RecordingObserver()
    owner_loop = asyncio.new_event_loop()

    def fail_if_provider_is_constructed(client: object) -> object:
        del client
        raise AssertionError("incompatible snapshots must stop before provider setup")

    try:
        result = AgentRuntimeFactory(
            object(),
            llm_factory=fail_if_provider_is_constructed,
            observer=observer,
        ).build(claimed_job, WorkerAsyncGateway(owner_loop))
    finally:
        owner_loop.close()

    assert isinstance(result, TerminalPreflightResult)
    assert result.reason == "snapshot_incompatible"
    assert result.routing_status is RoutingStatus.REJECTED
    terminal_events = [
        event
        for event in observer.events
        if event.event is EventName.RUNTIME_PREFLIGHT_COMPLETED
    ]
    assert terminal_events[-1].capability_error is CapabilityErrorCode.SNAPSHOT_INCOMPATIBLE


def test_compiled_profile_does_not_expose_mutable_configuration_mapping() -> None:
    config = load_tenant_config(EXAMPLES / "freight-broker.yaml")
    compiled = compile_tenant_profile(config, profile_fingerprint="b" * 64)

    assert compiled.config is not config
    field_name = next(iter(compiled.config.fields))
    with pytest.raises((TypeError, AttributeError)):
        compiled.config.fields["synthetic_field"] = compiled.config.fields[field_name]  # type: ignore[index]


def test_compiled_profile_does_not_expose_mutable_configuration_sequence() -> None:
    config = load_tenant_config(EXAMPLES / "freight-broker.yaml")
    compiled = compile_tenant_profile(config, profile_fingerprint="c" * 64)

    with pytest.raises((TypeError, AttributeError)):
        compiled.config.intake_types.append(compiled.config.intake_types[0])


class _FailingIntakeRepository:
    async def get_idempotency_for_tenant(
        self, tenant_id: UUID, key: str, *, for_update: bool
    ) -> None:
        del tenant_id, key, for_update
        return None

    async def create_job(
        self, command: EnqueueIntakeCommand, profile: ResolvedTenantProfile
    ) -> SimpleNamespace:
        del command, profile
        raise RuntimeError("synthetic enqueue persistence failure")


@pytest.mark.asyncio
async def test_enqueue_persistence_failure_keeps_known_profile_identity() -> None:
    profile = _profile()
    observer = RecordingObserver()
    service = IntakeEnqueueService(
        _MemorySession(),
        _StaticProfileResolver(profile),
        observer=observer,
        context=ObservationContext(trace_id=uuid4()),
    )
    service.repository = _FailingIntakeRepository()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="synthetic enqueue persistence failure"):
        await service.enqueue(
            EnqueueIntakeCommand(
                tenant_id=TENANT_ID,
                tenant_slug="freight-broker",
                source=TrustedSource(
                    channel="email",
                    subject="Synthetic request",
                    body="Synthetic envelope.",
                ),
                idempotency_key="enqueue-persistence-regression",
            )
        )

    event = next(
        event
        for event in observer.events
        if event.event is EventName.PERSISTENCE_OPERATION_FAILED
    )
    assert event.persistence_operation is PersistenceOperation.ENQUEUE_JOB
    assert event.scenario_key == profile.compiled.scenario_key  # type: ignore[union-attr]
    assert event.profile_fingerprint == profile.sha256


class _WorkerSession(AbstractAsyncContextManager["_WorkerSession"]):
    async def __aenter__(self) -> "_WorkerSession":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _WorkerSessionFactory:
    def __call__(self) -> _WorkerSession:
        return _WorkerSession()


class _FailingWorkerRepository:
    def __init__(self, session: object) -> None:
        del session

    async def mark_succeeded(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("synthetic worker persistence failure")


@pytest.mark.asyncio
async def test_worker_persistence_failure_keeps_known_profile_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    now = datetime.now(timezone.utc)
    claimed = ClaimedJob(
        id=uuid4(),
        tenant_id=TENANT_ID,
        trace_id=uuid4(),
        status="running",
        source_snapshot={
            "channel": "email",
            "subject": "Synthetic request",
            "body": "Synthetic envelope.",
        },
        tenant_config_snapshot=profile.snapshot,
        tenant_config_sha256=profile.sha256,
        risk_signals={"safety_or_legal_risk": False},
        attempt_count=1,
        side_effect_committed_at=None,
        created_at=now - timedelta(seconds=1),
        available_at=now - timedelta(seconds=1),
        started_at=now,
    )
    result = AgentRunResult(
        status=RunStatus.COMPLETED,
        reason=StopReason.FINAL,
        messages=(),
        steps=1,
        final_response="Synthetic result.",
    )
    observer = RecordingObserver()
    monkeypatch.setattr(agent_worker_module, "JobRepository", _FailingWorkerRepository)
    worker = AgentWorker(_WorkerSessionFactory(), observer=observer)

    with pytest.raises(RuntimeError, match="synthetic worker persistence failure"):
        await worker._persist_result(claimed, result)

    event = next(
        event
        for event in observer.events
        if event.event is EventName.PERSISTENCE_OPERATION_FAILED
    )
    assert event.persistence_operation is PersistenceOperation.MARK_SUCCEEDED
    assert event.scenario_key == profile.compiled.scenario_key  # type: ignore[union-attr]
    assert event.profile_fingerprint == profile.sha256
