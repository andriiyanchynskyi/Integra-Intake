"""Provider-free execution of the deterministic freight evaluation corpus."""

from __future__ import annotations

import base64
from collections import deque
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from app.agent import AgentLoop, AgentMessage, AgentProposal, MessageRole
from app.auth.inbound_webhook import (
    sign_inbound_webhook,
    verify_inbound_webhook_signature,
)
from app.documents import (
    DocumentNormalizer,
    DocumentMediaType,
    RateConfirmationDocumentInput,
)
from app.domain.intake import (
    EnqueueIntakeCommand,
    EnqueueResult,
    IdempotencyConflict,
    IntakeEnqueueService,
)
from app.inbound import (
    InboundIntakeService,
    WebhookAttachmentPayload,
    WebhookEmailPayload,
    parse_webhook_email_payload,
)
from app.policy import (
    PolicyEngine,
    PolicyInput,
    PolicyOutcome,
    RiskSignals,
    TrustedSource,
    TrustedToolRuntimeContext,
)
from app.runtime.factory import AgentRuntimeFactory
from app.runtime.profiles import TenantProfileResolver
from app.tenants.config import TenantConfig
from app.tools.executor import PolicyGatedToolExecutor
from app.tools.in_memory import InMemoryTenantToolPort

from .loader import ResolvedDocumentFixture
from .models import (
    EvalCaseKind,
    FreightBodySource,
    FreightDocumentSource,
    FreightEvalCase,
    FreightEvalCaseResult,
    FreightEvalExpected,
    FreightEvalObservation,
    FreightWebhookSource,
)


class FreightEvalExecutionError(RuntimeError):
    """A safe, stable scripted-evaluation execution failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ScriptedLLM:
    """Return a fixed typed proposal sequence without inspecting messages."""

    def __init__(self, proposals: Sequence[AgentProposal]) -> None:
        validated = tuple(AgentProposal.model_validate(item) for item in proposals)
        self._proposals = deque(deepcopy(validated))
        self.calls = 0
        self.returned_proposals: list[AgentProposal] = []

    def complete(self, messages: Sequence[AgentMessage]) -> AgentProposal:
        del messages
        self.calls += 1
        if not self._proposals:
            raise FreightEvalExecutionError("scripted_proposals_exhausted")
        proposal = deepcopy(self._proposals.popleft())
        self.returned_proposals.append(deepcopy(proposal))
        return proposal

    def assert_consumed(self) -> None:
        if self._proposals:
            raise FreightEvalExecutionError("scripted_proposals_unused")


class RecordingPolicy:
    """Delegate policy decisions to the production engine and record outcomes."""

    def __init__(self) -> None:
        self._delegate = PolicyEngine()
        self.outcomes: list[PolicyOutcome] = []

    def evaluate(self, value: PolicyInput) -> PolicyOutcome:
        outcome = self._delegate.evaluate(value)
        self.outcomes.append(outcome)
        return outcome


class _RecordingLLM:
    """Record a real local fallback client without changing its behavior."""

    def __init__(self, delegate: object) -> None:
        self._delegate = delegate
        self.calls = 0
        self.returned_proposals: list[AgentProposal] = []

    def complete(self, messages: Sequence[AgentMessage]) -> AgentProposal:
        self.calls += 1
        proposal = AgentProposal.model_validate(self._delegate.complete(messages))
        self.returned_proposals.append(deepcopy(proposal))
        return proposal


class _ProviderCallGuard:
    """Fail closed if the runtime tries to construct a real provider client."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, client: object) -> object:
        del client
        self.calls += 1
        raise FreightEvalExecutionError("provider_called")


@dataclass(frozen=True, slots=True)
class _MemoryIdempotencyRecord:
    tenant_id: UUID
    key: str
    request_hash: str
    job_id: UUID


@dataclass(frozen=True, slots=True)
class _MemoryJob:
    id: UUID


class _AsyncTransaction:
    async def __aenter__(self) -> _AsyncTransaction:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback


class _MemoryIntakeSession:
    def begin(self) -> _AsyncTransaction:
        return _AsyncTransaction()

    def begin_nested(self) -> _AsyncTransaction:
        return _AsyncTransaction()

    async def flush(self) -> None:
        return None


class _MemoryIntakeRepository:
    """Minimal persistence seam used only through IntakeEnqueueService."""

    def __init__(self) -> None:
        self.idempotency: dict[tuple[UUID, str], _MemoryIdempotencyRecord] = {}
        self.jobs: dict[UUID, EnqueueIntakeCommand] = {}

    async def get_idempotency_for_tenant(
        self, tenant_id: UUID, key: str, *, for_update: bool = False
    ) -> _MemoryIdempotencyRecord | None:
        del for_update
        return self.idempotency.get((tenant_id, key))

    async def create_job(
        self, command: EnqueueIntakeCommand, profile: object
    ) -> _MemoryJob:
        del profile
        job = _MemoryJob(id=uuid4())
        self.jobs[job.id] = deepcopy(command)
        return job

    async def create_idempotency_record(
        self,
        tenant_id: UUID,
        key: str,
        request_hash: str,
        job_id: UUID,
    ) -> _MemoryIdempotencyRecord:
        record = _MemoryIdempotencyRecord(
            tenant_id=tenant_id,
            key=key,
            request_hash=request_hash,
            job_id=job_id,
        )
        self.idempotency[(tenant_id, key)] = record
        return record


class _RecordingEnqueue:
    def __init__(self, result: EnqueueResult | None = None) -> None:
        self.commands: list[EnqueueIntakeCommand] = []
        self._result = result

    async def enqueue(self, command: EnqueueIntakeCommand) -> EnqueueResult:
        self.commands.append(deepcopy(command))
        return self._result or EnqueueResult(job_id=uuid4(), created=True)


_EVAL_WEBHOOK_SIGNING_KEY = "ik_" + ("A" * 43)
_EVAL_WEBHOOK_TIMESTAMP = 1_800_000_000


def _verified_webhook_payload(payload: WebhookEmailPayload) -> WebhookEmailPayload:
    """Exercise exact-body HMAC verification before mapping the payload."""

    raw_body = payload.model_dump_json().encode("utf-8")
    signature = sign_inbound_webhook(
        _EVAL_WEBHOOK_SIGNING_KEY,
        _EVAL_WEBHOOK_TIMESTAMP,
        raw_body,
    )
    if not verify_inbound_webhook_signature(
        _EVAL_WEBHOOK_SIGNING_KEY,
        str(_EVAL_WEBHOOK_TIMESTAMP),
        signature,
        raw_body,
        now=_EVAL_WEBHOOK_TIMESTAMP,
    ):
        raise FreightEvalExecutionError("signature_verification_mismatch")
    try:
        return parse_webhook_email_payload(raw_body)
    except ValueError as error:
        raise FreightEvalExecutionError("signature_verification_mismatch") from error


def _body_source(source: FreightBodySource) -> TrustedSource:
    return TrustedSource(
        channel=source.channel,
        subject=source.subject,
        body=source.body,
    )


def _read_document_fixture(
    source: FreightDocumentSource | FreightWebhookSource,
    fixture: ResolvedDocumentFixture,
) -> tuple[str | None, bytes | None]:
    del source
    raw = fixture.path.read_bytes()
    if fixture.media_type is DocumentMediaType.TEXT:
        return raw.decode("utf-8"), None
    return None, raw


def _document_source(
    source: FreightDocumentSource,
    fixture: ResolvedDocumentFixture,
) -> TrustedSource:
    text, content = _read_document_fixture(source, fixture)
    normalized = DocumentNormalizer().normalize(
        RateConfirmationDocumentInput(
            channel=source.channel,
            subject=source.subject,
            body=source.body,
            media_type=fixture.media_type,
            text=text,
            content=content,
        )
    )
    return TrustedSource(
        channel=source.channel,
        subject=source.subject,
        body=source.body,
        document=normalized,
    )


async def _webhook_source(
    source: FreightWebhookSource,
    *,
    fixture: ResolvedDocumentFixture | None,
    tenant_id: UUID,
    tenant_slug: str,
) -> tuple[TrustedSource, bool]:
    attachments: list[WebhookAttachmentPayload] = []
    if fixture is not None:
        raw = fixture.path.read_bytes()
        attachments.append(
            WebhookAttachmentPayload(
                media_type=fixture.media_type.value,
                content_base64=base64.b64encode(raw).decode("ascii"),
            )
        )
    payload = WebhookEmailPayload(
        provider_id=source.provider_id,
        from_addr=source.from_addr,
        subject=source.subject,
        body=source.body,
        attachments=attachments,
    )
    verified_payload = _verified_webhook_payload(payload)
    message = verified_payload.to_message(tenant_id, tenant_slug)
    recorder = _RecordingEnqueue()
    await InboundIntakeService(recorder).enqueue(message)
    if len(recorder.commands) != 1:
        raise FreightEvalExecutionError("webhook_source_unavailable")
    return recorder.commands[0].source, True


def _fixture_for(
    fixture_id: str | None,
    fixtures: Mapping[str, ResolvedDocumentFixture],
) -> ResolvedDocumentFixture | None:
    if fixture_id is None:
        return None
    fixture = fixtures.get(fixture_id)
    if fixture is None:
        raise FreightEvalExecutionError("document_fixture_unavailable")
    return fixture


async def _source_for_case(
    case: FreightEvalCase,
    *,
    tenant_id: UUID,
    tenant_config: TenantConfig,
    document_fixtures: Mapping[str, ResolvedDocumentFixture],
) -> tuple[TrustedSource, bool]:
    source = case.source
    if isinstance(source, FreightBodySource):
        return _body_source(source), False
    if isinstance(source, FreightDocumentSource):
        fixture = _fixture_for(source.fixture_id, document_fixtures)
        if fixture is None:
            raise FreightEvalExecutionError("document_fixture_unavailable")
        return _document_source(source, fixture), False
    fixture = _fixture_for(source.document_fixture_id, document_fixtures)
    return await _webhook_source(
        source,
        fixture=fixture,
        tenant_id=tenant_id,
        tenant_slug=tenant_config.slug if tenant_config else "freight-broker",
    )


def _first_tool_name(messages: Sequence[AgentMessage]) -> str | None:
    for message in messages:
        if message.role is MessageRole.ASSISTANT and message.tool_call is not None:
            return message.tool_call.name
    return None


def _agent_observation(
    case: FreightEvalCase,
    *,
    source: TrustedSource,
    tenant_config: TenantConfig,
    signature_verified: bool,
) -> tuple[FreightEvalObservation, tuple[str, ...]]:
    tenant_id = uuid5(NAMESPACE_URL, f"https://integra.invalid/freight/{case.id}")
    policy = RecordingPolicy()
    port = InMemoryTenantToolPort()
    runtime = TrustedToolRuntimeContext(
        tenant_id=tenant_id,
        tenant_config=tenant_config,
        source=source,
        risk_signals=RiskSignals(),
        job_id=tenant_id,
    )
    provider_guard = _ProviderCallGuard()
    runtime_factory = AgentRuntimeFactory(object(), llm_factory=provider_guard)
    unreadable_document = (
        source.document is not None and source.document.extraction_error is not None
    )
    if unreadable_document:
        llm = _RecordingLLM(runtime_factory.create_llm(source, object()))
    else:
        llm = ScriptedLLM(case.scripted_proposals)
    executor = PolicyGatedToolExecutor(runtime=runtime, port=port, policy=policy)
    loop = AgentLoop(llm, executor)
    initial_messages = AgentRuntimeFactory.initial_messages(tenant_config, source)
    extra_mismatches: list[str] = []
    run_result = None
    try:
        run_result = loop.run(initial_messages)
        try:
            if isinstance(llm, ScriptedLLM):
                llm.assert_consumed()
        except FreightEvalExecutionError as error:
            extra_mismatches.append(error.code)
    except FreightEvalExecutionError as error:
        extra_mismatches.append(error.code)

    if unreadable_document and case.scripted_proposals:
        if not llm.returned_proposals or llm.returned_proposals[0] != case.scripted_proposals[0]:
            extra_mismatches.append("unreadable_fallback_mismatch")
    first_proposal = llm.returned_proposals[0] if llm.returned_proposals else None
    outcome = policy.outcomes[-1] if policy.outcomes else None
    missing = (
        tuple(sorted(outcome.missing_required_fields)) if outcome is not None else None
    )
    observation = FreightEvalObservation(
        intake_type=first_proposal.intake_type if first_proposal else None,
        missing_required_fields=missing,
        policy_decision=(outcome.decision.value if outcome is not None else None),
        routing_status=(outcome.status.value if outcome is not None else None),
        routing_reason=(outcome.reason if outcome is not None else None),
        tool_name=_first_tool_name(run_result.messages if run_result is not None else ()),
        approval_required=bool(port.approval_requests),
        case_created=bool(port.cases),
        signature_verified=signature_verified,
        document_extraction_error=(
            source.document.extraction_error.value
            if source.document is not None and source.document.extraction_error is not None
            else None
        ),
        provider_calls=provider_guard.calls,
        llm_calls=llm.calls,
        steps=run_result.steps if run_result is not None else None,
        stop_reason=run_result.reason.value if run_result is not None else None,
    )
    return observation, tuple(extra_mismatches)


def _compare(
    observation: FreightEvalObservation,
    expected: FreightEvalExpected,
    extra: Sequence[str] = (),
) -> tuple[str, ...]:
    fields = (
        ("intake_type", "intake_type_mismatch"),
        ("missing_required_fields", "missing_fields_mismatch"),
        ("policy_decision", "policy_decision_mismatch"),
        ("routing_status", "routing_status_mismatch"),
        ("routing_reason", "routing_reason_mismatch"),
        ("tool_name", "tool_name_mismatch"),
        ("approval_required", "approval_mismatch"),
        ("case_created", "case_side_effect_mismatch"),
        ("duplicate_reused", "duplicate_reuse_mismatch"),
        ("conflict_raised", "idempotency_conflict_mismatch"),
        ("tenant_isolated", "tenant_isolation_mismatch"),
        ("signature_verified", "signature_verification_mismatch"),
        ("document_extraction_error", "document_extraction_error_mismatch"),
        ("provider_calls", "provider_calls_mismatch"),
        ("llm_calls", "llm_calls_mismatch"),
        ("steps", "steps_mismatch"),
        ("stop_reason", "stop_reason_mismatch"),
    )
    mismatches = list(extra)
    for field_name, code in fields:
        expected_value = getattr(expected, field_name)
        if expected_value is not None and getattr(observation, field_name) != expected_value:
            mismatches.append(code)
    return tuple(sorted(set(mismatches)))


def _result(
    case: FreightEvalCase,
    observation: FreightEvalObservation,
    extra_mismatches: Sequence[str] = (),
) -> FreightEvalCaseResult:
    mismatches = _compare(observation, case.expected, extra_mismatches)
    return FreightEvalCaseResult(
        id=case.id,
        category=case.category,
        passed=not mismatches,
        observation=observation,
        expected=case.expected,
        mismatches=mismatches,
    )


def _blank_observation() -> FreightEvalObservation:
    return FreightEvalObservation(provider_calls=0, llm_calls=0)


async def _run_agent_case(
    case: FreightEvalCase,
    *,
    tenant_config: TenantConfig,
    document_fixtures: Mapping[str, ResolvedDocumentFixture],
) -> FreightEvalCaseResult:
    tenant_id = uuid5(NAMESPACE_URL, f"https://integra.invalid/freight/{case.id}")
    try:
        source, signature_verified = await _source_for_case(
            case,
            tenant_id=tenant_id,
            tenant_config=tenant_config,
            document_fixtures=document_fixtures,
        )
        observation, extra = _agent_observation(
            case,
            source=source,
            tenant_config=tenant_config,
            signature_verified=signature_verified,
        )
        return _result(case, observation, extra)
    except FreightEvalExecutionError as error:
        return _result(case, _blank_observation(), (error.code,))
    except (OSError, UnicodeError, ValueError):
        return _result(case, _blank_observation(), ("source_execution_failed",))


async def _run_webhook_invariant(
    case: FreightEvalCase,
    *,
    tenant_config: TenantConfig,
    document_fixtures: Mapping[str, ResolvedDocumentFixture],
) -> FreightEvalCaseResult:
    source = case.source
    tenant_a = uuid5(NAMESPACE_URL, f"https://integra.invalid/tenant/{case.id}/a")
    tenant_b = uuid5(NAMESPACE_URL, f"https://integra.invalid/tenant/{case.id}/b")
    if isinstance(source, FreightWebhookSource):
        fixture = _fixture_for(source.document_fixture_id, document_fixtures)
    else:
        fixture = None
    repository = _MemoryIntakeRepository()
    session = _MemoryIntakeSession()
    profile_resolver = TenantProfileResolver(Path(__file__).resolve().parents[2] / "examples")
    service = IntakeEnqueueService(session, profile_resolver)
    service.repository = repository  # eval-only seam; production service is unchanged

    async def enqueue_for(tenant_id: UUID, body: str) -> EnqueueResult:
        if not isinstance(source, FreightWebhookSource):
            raise FreightEvalExecutionError("invalid_invariant_source")
        payload = WebhookEmailPayload(
            provider_id=source.provider_id,
            from_addr=source.from_addr,
            subject=source.subject,
            body=body,
            attachments=(
                [
                    WebhookAttachmentPayload(
                        media_type=fixture.media_type.value,
                        content_base64=base64.b64encode(fixture.path.read_bytes()).decode(
                            "ascii"
                        ),
                    )
                ]
                if fixture is not None
                else []
            ),
        )
        message = _verified_webhook_payload(payload).to_message(
            tenant_id, tenant_config.slug
        )
        return await InboundIntakeService(service).enqueue(message)

    duplicate_reused = False
    conflict_raised = False
    signature_verified = False
    if isinstance(source, FreightWebhookSource):
        try:
            first = await enqueue_for(tenant_a, source.body)
            signature_verified = True
            second_tenant = tenant_b if source.second_tenant else tenant_a
            second_body = (
                source.second_body if source.second_body is not None else source.body
            )
            second = await enqueue_for(second_tenant, second_body)
            duplicate_reused = not second.created and first.job_id == second.job_id
        except IdempotencyConflict:
            conflict_raised = True

    tenant_isolated: bool | None = None
    if case.kind is EvalCaseKind.TENANT_ISOLATION:
        port = InMemoryTenantToolPort()
        customer = port.add_customer(tenant_b, email="tenant-b@example.test")
        case_id = port.seed_case(tenant_b)
        tenant_isolated = (
            port.find_customer(tenant_a, email=customer.email, external_id=None) is None
            and port.update_case_fields(tenant_a, case_id, fields={"origin": "x"})
            is None
            and not port.case_exists(tenant_a, case_id)
        )
        if isinstance(source, FreightWebhookSource) and source.second_tenant:
            tenant_isolated = tenant_isolated and len(repository.jobs) == 2

    observation = FreightEvalObservation(
        duplicate_reused=duplicate_reused,
        conflict_raised=conflict_raised,
        tenant_isolated=tenant_isolated,
        signature_verified=signature_verified,
        provider_calls=0,
        llm_calls=0,
    )
    return _result(case, observation)


async def run_freight_eval_case(
    case: FreightEvalCase,
    *,
    tenant_config: TenantConfig,
    document_fixtures: Mapping[str, ResolvedDocumentFixture],
) -> FreightEvalCaseResult:
    """Execute one case using only typed fakes and current application seams."""

    if case.kind is EvalCaseKind.AGENT:
        return await _run_agent_case(
            case,
            tenant_config=tenant_config,
            document_fixtures=document_fixtures,
        )
    try:
        return await _run_webhook_invariant(
            case,
            tenant_config=tenant_config,
            document_fixtures=document_fixtures,
        )
    except FreightEvalExecutionError as error:
        return _result(case, _blank_observation(), (error.code,))


__all__ = [
    "FreightEvalExecutionError",
    "RecordingPolicy",
    "ScriptedLLM",
    "run_freight_eval_case",
]
