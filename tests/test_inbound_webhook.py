"""Offline tests for the Phase-10 inbound webhook contracts and service."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.inbound import get_inbound_intake_service
from app.auth import get_current_inbound_tenant, get_current_tenant
from app.auth import sign_inbound_webhook
from app.agent import (
    MAX_STEPS,
    AgentLoop,
    AgentMessage,
    AgentProposal,
    AgentRunResult,
    MessageRole,
    ProposalPriority,
    ProposalToolCall,
    RunStatus,
    StopReason,
    ToolCall,
    ToolExecutionResult,
)
from app.db.models import Tenant
from app.db.session import get_db_session
from app.documents import (
    DocumentExtractionError,
    DocumentMediaType,
    MAX_DOCUMENT_BYTES,
    NormalizedRateConfirmationDocument,
    RateConfirmationDocumentInput,
)
from app.domain.intake import (
    EnqueueIntakeCommand,
    EnqueueResult,
    IdempotencyConflict,
)
from app.domain.job_repository import JobRepository
from app.inbound import (
    InvalidInboundPayload,
    MAX_INBOUND_WEBHOOK_BODY_BYTES,
    InboundMessage,
    parse_webhook_email_payload,
)
from app.inbound.service import InboundIntakeService
from app.main import app
from app.policy import TrustedToolRuntimeContext
from app.runtime.factory import AgentRuntimeFactory
from app.runtime.profiles import TenantProfileUnavailableError
from app.tenants.loader import load_tenant_config
from app.tools.executor import PolicyGatedToolExecutor
from app.workers.agent_worker import AgentWorker
from scripts.send_inbound_webhook import _attachment_payload, build_request


@dataclass
class _RecordingEnqueueService:
    """Small async double for the existing enqueue boundary."""

    job_id: UUID

    def __post_init__(self) -> None:
        self.commands: list[EnqueueIntakeCommand] = []

    async def enqueue(self, command: EnqueueIntakeCommand) -> EnqueueResult:
        self.commands.append(command)
        return EnqueueResult(job_id=self.job_id, created=True)


class _RecordingInboundService:
    """HTTP-test double at the inbound service boundary."""

    def __init__(self, job_id: UUID | None = None) -> None:
        self.job_id = job_id or uuid4()
        self.messages: list[InboundMessage] = []
        self.error: Exception | None = None

    async def enqueue(self, message: InboundMessage) -> EnqueueResult:
        self.messages.append(message)
        if self.error is not None:
            raise self.error
        return EnqueueResult(job_id=self.job_id, created=True)


class _FakeNormalizer:
    def __init__(self, result: NormalizedRateConfirmationDocument) -> None:
        self.result = result
        self.inputs: list[RateConfirmationDocumentInput] = []

    def normalize(
        self, value: RateConfirmationDocumentInput
    ) -> NormalizedRateConfirmationDocument:
        self.inputs.append(value)
        return self.result


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "provider_id": "provider-123",
        "from_addr": "dispatcher@example.test",
        "subject": "Load",
        "body": "Need a truck",
        "attachments": [],
    }
    payload.update(overrides)
    return payload


def _parse(payload: dict[str, Any]):
    return parse_webhook_email_payload(json.dumps(payload).encode("utf-8"))


def _attachment(
    *, media_type: str = "application/pdf", content: bytes = b"%PDF-raw-secret"
) -> dict[str, str]:
    return {
        "media_type": media_type,
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def _message(payload: dict[str, Any], *, tenant_id: UUID | None = None) -> InboundMessage:
    return _parse(payload).to_message(tenant_id or uuid4(), "freight-broker")


def test_local_sender_build_request_has_exact_signed_envelope_without_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_key = "ik_local-script-secret"
    raw_attachment = b"Origin: Kyiv\nDestination: Lviv\n"
    attachment = tmp_path / "rate-confirmation.txt"
    attachment.write_bytes(raw_attachment)
    timestamp = 1_700_000_000

    request = build_request(
        "http://127.0.0.1:8000/v1/inbound/email/webhook",
        api_key=raw_key,
        provider_id="local-msg-0001",
        from_addr="dispatcher@example.test",
        subject="Rate confirmation",
        body="Please process the attachment.",
        attachment=attachment,
        timestamp=timestamp,
    )

    expected_payload = {
        "provider_id": "local-msg-0001",
        "from_addr": "dispatcher@example.test",
        "subject": "Rate confirmation",
        "body": "Please process the attachment.",
        "attachments": [
            {
                "media_type": "text/plain",
                "content_base64": base64.b64encode(raw_attachment).decode("ascii"),
            }
        ],
    }
    expected_body = json.dumps(
        expected_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    assert request.full_url == "http://127.0.0.1:8000/v1/inbound/email/webhook"
    assert request.method == "POST"
    assert request.data == expected_body
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers == {
        "content-type": "application/json",
        "x-api-key": raw_key,
        "x-inbound-timestamp": str(timestamp),
        "x-inbound-signature": sign_inbound_webhook(raw_key, timestamp, expected_body),
    }
    assert capsys.readouterr().out == ""
    assert [path.name for path in tmp_path.iterdir()] == [attachment.name]


def test_local_sender_rejects_oversized_attachment_before_reading_bytes() -> None:
    class OversizedAttachment:
        suffix = ".pdf"

        def stat(self) -> object:
            return type("StatResult", (), {"st_size": MAX_DOCUMENT_BYTES + 1})()

        def read_bytes(self) -> bytes:
            raise AssertionError("oversized attachment must not be read")

    with pytest.raises(ValueError, match="attachment exceeds the document size limit"):
        _attachment_payload(OversizedAttachment())  # type: ignore[arg-type]


@pytest.fixture
def inbound_client() -> tuple[TestClient, Tenant, _RecordingInboundService]:
    tenant = Tenant(id=uuid4(), slug="freight-broker", name="Freight broker")
    service = _RecordingInboundService()

    async def override_inbound_tenant() -> Tenant:
        return tenant

    async def override_legacy_tenant() -> Tenant:
        return tenant

    def override_inbound_service() -> _RecordingInboundService:
        return service

    async def override_session():
        yield object()

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_current_inbound_tenant] = override_inbound_tenant
    app.dependency_overrides[get_current_tenant] = override_legacy_tenant
    app.dependency_overrides[get_inbound_intake_service] = override_inbound_service
    app.dependency_overrides[get_db_session] = override_session
    try:
        with TestClient(app) as client:
            yield client, tenant, service
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


def test_blank_subject_is_normalized_and_tenant_and_channel_are_server_owned() -> None:
    tenant_id = uuid4()
    message = _message(
        _payload(subject=" \t"),
        tenant_id=tenant_id,
    )

    assert message.tenant_id == tenant_id
    assert message.tenant_slug == "freight-broker"
    assert message.channel == "email_webhook"
    assert message.subject == "(no subject)"
    assert message.from_addr == "dispatcher@example.test"


@pytest.mark.parametrize(
    "extra_field",
    [
        ("tenant_id", str(uuid4())),
        ("channel", "other-channel"),
        ("filename", "rate-confirmation.pdf"),
    ],
)
def test_unknown_webhook_fields_raise_one_sanitized_error(
    extra_field: tuple[str, str],
) -> None:
    field, value = extra_field
    payload = _payload()
    payload[field] = value

    with pytest.raises(InvalidInboundPayload) as error:
        _parse(payload)

    assert str(error.value) == "invalid inbound payload"
    assert field not in str(error.value)


def test_malformed_json_is_sanitized_at_public_parser_boundary() -> None:
    with pytest.raises(InvalidInboundPayload) as error:
        parse_webhook_email_payload(b"{not-json")

    assert str(error.value) == "invalid inbound payload"


def test_provider_id_with_surrounding_whitespace_is_rejected() -> None:
    with pytest.raises(InvalidInboundPayload) as error:
        _parse(_payload(provider_id=" provider-123 "))

    assert str(error.value) == "invalid inbound payload"


def test_body_only_message_is_accepted_without_an_attachment() -> None:
    message = _message(_payload())

    assert message.attachments == ()
    assert message.body == "Need a truck"


@pytest.mark.parametrize(
    "payload",
    [
        _payload(body="", attachments=[]),
        _payload(body="   ", attachments=[]),
        _payload(attachments=[_attachment(content=b"")]),
        _payload(attachments=[{"media_type": "application/pdf", "content_base64": "%%%"}]),
        _payload(
            attachments=[
                _attachment(media_type="text/plain", content=b"\xff")
            ]
        ),
    ],
)
def test_invalid_body_or_attachment_payloads_are_rejected(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(InvalidInboundPayload) as error:
        _message(payload)

    assert str(error.value) == "invalid inbound payload"


def test_unsupported_mime_is_rejected_by_closed_wire_contract() -> None:
    with pytest.raises(InvalidInboundPayload) as error:
        _parse(_payload(attachments=[_attachment(media_type="image/png")]))

    assert str(error.value) == "invalid inbound payload"


def test_more_than_one_attachment_is_rejected() -> None:
    payload = _payload(attachments=[_attachment(), _attachment(content=b"second")])

    with pytest.raises(InvalidInboundPayload) as error:
        _parse(payload)

    assert str(error.value) == "invalid inbound payload"


def test_decoded_attachment_over_document_limit_is_rejected() -> None:
    oversized = b"x" * (MAX_DOCUMENT_BYTES + 1)
    payload = _payload(attachments=[_attachment(content=oversized)])

    with pytest.raises(InvalidInboundPayload) as error:
        _message(payload)

    assert str(error.value) == "invalid inbound payload"


def test_raw_webhook_body_limit_is_public_and_larger_than_document_limit() -> None:
    assert MAX_INBOUND_WEBHOOK_BODY_BYTES > MAX_DOCUMENT_BYTES


@pytest.mark.asyncio
async def test_body_only_service_enqueues_existing_command_with_provider_id_key() -> None:
    enqueue = _RecordingEnqueueService(job_id=uuid4())
    service = InboundIntakeService(enqueue)
    message = _message(_payload())

    result = await service.enqueue(message)

    assert result == EnqueueResult(job_id=enqueue.job_id, created=True)
    assert len(enqueue.commands) == 1
    command = enqueue.commands[0]
    assert isinstance(command, EnqueueIntakeCommand)
    assert command.tenant_id == message.tenant_id
    assert command.tenant_slug == "freight-broker"
    assert command.idempotency_key == "email_webhook:provider-123"
    assert command.source.channel == "email_webhook"
    assert command.source.sender == "dispatcher@example.test"
    assert command.source.subject == "Load"
    assert command.source.body == "Need a truck"
    assert command.source.document is None


@pytest.mark.asyncio
async def test_pdf_attachment_is_normalized_only_through_injected_to_thread_and_safe_source(
) -> None:
    raw_pdf = b"%PDF-raw-secret"
    encoded = base64.b64encode(raw_pdf).decode("ascii")
    message = _message(
        _payload(
            subject="Rate confirmation",
            body="Please process the attachment.",
            attachments=[_attachment(content=raw_pdf)],
        )
    )
    normalized = NormalizedRateConfirmationDocument(
        media_type=DocumentMediaType.PDF,
        sha256="a" * 64,
        text="Origin: Chicago\nDestination: Detroit",
    )
    normalizer = _FakeNormalizer(normalized)
    thread_calls: list[tuple[object, tuple[object, ...]]] = []

    async def injected_to_thread(function: object, *args: object) -> object:
        thread_calls.append((function, args))
        return function(*args)  # type: ignore[operator]

    enqueue = _RecordingEnqueueService(job_id=uuid4())
    service = InboundIntakeService(
        enqueue,
        normalizer=normalizer,
        to_thread=injected_to_thread,
    )

    await service.enqueue(message)

    assert len(thread_calls) == 1
    assert getattr(thread_calls[0][0], "__self__", None) is normalizer
    assert len(normalizer.inputs) == 1
    document_input = normalizer.inputs[0]
    assert document_input.media_type is DocumentMediaType.PDF
    assert document_input.content == raw_pdf
    assert document_input.body == "Please process the attachment."

    assert len(enqueue.commands) == 1
    command = enqueue.commands[0]
    assert command.source.body == "Please process the attachment."
    assert command.source.sender == "dispatcher@example.test"
    assert command.source.document == normalized
    source_repr = repr(command.source)
    assert raw_pdf not in source_repr.encode("utf-8")
    assert encoded not in source_repr
    assert "content" not in normalized.model_dump(mode="json")


@pytest.mark.asyncio
async def test_malformed_normalizer_result_is_still_enqueued_as_document_source() -> None:
    message = _message(_payload(attachments=[_attachment()]))
    malformed = NormalizedRateConfirmationDocument(
        media_type=DocumentMediaType.PDF,
        sha256="b" * 64,
        extraction_error=DocumentExtractionError.PDF_MALFORMED,
    )
    normalizer = _FakeNormalizer(malformed)
    enqueue = _RecordingEnqueueService(job_id=uuid4())

    async def injected_to_thread(function: object, *args: object) -> object:
        return function(*args)  # type: ignore[operator]

    service = InboundIntakeService(
        enqueue,
        normalizer=normalizer,
        to_thread=injected_to_thread,
    )

    result = await service.enqueue(message)

    assert result.created is True
    assert len(enqueue.commands) == 1
    document = enqueue.commands[0].source.document
    assert document == malformed
    assert document is not None
    assert document.text is None
    assert document.extraction_error is DocumentExtractionError.PDF_MALFORMED


@pytest.mark.asyncio
async def test_attachment_boundary_keeps_only_normalized_safe_metadata_downstream() -> None:
    raw_key = "ik_attachment-secret"
    provider_id = "provider-private-42"
    filename = "rate-confirmation-private.pdf"
    parser_exception = "PdfReadError: parser details must not persist"
    raw_pdf = b"%PDF-raw-private-attachment"
    encoded = base64.b64encode(raw_pdf).decode("ascii")
    message = _message(
        _payload(
            provider_id=provider_id,
            attachments=[_attachment(content=raw_pdf)],
        )
    )
    normalized = NormalizedRateConfirmationDocument(
        media_type=DocumentMediaType.PDF,
        sha256="c" * 64,
        extraction_error=DocumentExtractionError.PDF_MALFORMED,
    )
    enqueue = _RecordingEnqueueService(job_id=uuid4())
    normalizer = _FakeNormalizer(normalized)

    async def injected_to_thread(function: object, *args: object) -> object:
        return function(*args)  # type: ignore[operator]

    await InboundIntakeService(
        enqueue,
        normalizer=normalizer,
        to_thread=injected_to_thread,
    ).enqueue(message)

    source = enqueue.commands[0].source
    source_snapshot = JobRepository._source_snapshot(source)
    snapshot_text = repr(source_snapshot)
    assert source_snapshot["document"] == normalized.model_dump(mode="json")
    assert "content" not in source_snapshot["document"]  # type: ignore[operator]
    assert normalized.sha256 in snapshot_text  # safe idempotency metadata is retained
    for secret in (raw_pdf, encoded, filename, provider_id, raw_key, parser_exception):
        assert (
            secret not in snapshot_text.encode("utf-8")
            if isinstance(secret, bytes)
            else secret not in snapshot_text
        )

    profile = load_tenant_config(
        Path(__file__).resolve().parents[1] / "examples" / "freight-broker.yaml"
    )
    messages = AgentRuntimeFactory._initial_messages(profile, source)
    prompt_text = "\n".join(message.content or "" for message in messages)
    assert normalized.sha256 not in prompt_text
    assert "document_unreadable" not in prompt_text
    for secret in (encoded, filename, provider_id, raw_key, parser_exception):
        assert secret not in prompt_text
    assert raw_pdf not in prompt_text.encode("utf-8")

    result = AgentRunResult(
        status=RunStatus.COMPLETED,
        reason=StopReason.EXECUTOR_STOPPED,
        messages=(AgentMessage(role=MessageRole.TOOL, tool_result=None),),
        steps=1,
        final_response="document_unreadable",
    )
    summary_text = repr(AgentWorker._result_summary(result))
    for secret in (encoded, filename, provider_id, normalized.sha256, raw_key, parser_exception):
        assert secret not in summary_text
    assert raw_pdf not in summary_text.encode("utf-8")


@pytest.mark.asyncio
async def test_inbound_source_uses_existing_sync_loop_and_policy_boundaries() -> None:
    message = _message(_payload(provider_id="provider-loop-1"))
    enqueue = _RecordingEnqueueService(job_id=uuid4())
    await InboundIntakeService(enqueue).enqueue(message)
    source = enqueue.commands[0].source
    profile = load_tenant_config(
        Path(__file__).resolve().parents[1] / "examples" / "freight-broker.yaml"
    )
    initial_messages = AgentRuntimeFactory._initial_messages(profile, source)

    def proposal(tool_name: str | None) -> AgentProposal:
        return AgentProposal.model_validate(
            {
                "intake_type": "other",
                "fields": [],
                "missing_required_fields": [],
                "priority": ProposalPriority.NORMAL,
                "contains_injection_or_override_attempt": False,
                "rationale_short": "structured inbound proposal",
                "tool_calls": (
                    []
                    if tool_name is None
                    else [
                        {
                            "name": tool_name,
                            "arguments": [
                                {
                                    "name": "email",
                                    "value": "dispatcher@example.test",
                                },
                                {"name": "external_id", "value": None},
                            ],
                        }
                    ]
                ),
                "confidence": 0.5,
            }
        )

    class _SequenceLLM:
        def __init__(self, proposals: list[AgentProposal]) -> None:
            self.proposals = proposals

        def complete(self, messages: object) -> AgentProposal:
            del messages
            return self.proposals.pop(0)

    class _PolicyPort:
        def __init__(self) -> None:
            self.tenant_ids: list[UUID] = []

        def find_customer(
            self,
            tenant_id: UUID,
            *,
            email: str | None,
            external_id: str | None,
        ) -> None:
            del email, external_id
            self.tenant_ids.append(tenant_id)
            return None

    policy_port = _PolicyPort()
    policy_executor = PolicyGatedToolExecutor(
        runtime=TrustedToolRuntimeContext(
            tenant_id=message.tenant_id,
            tenant_config=profile,
            source=source,
        ),
        port=policy_port,
    )
    policy_result = AgentLoop(
        _SequenceLLM([proposal("find_customer"), proposal(None)]),
        policy_executor,
    ).run(initial_messages)
    assert policy_result.reason is StopReason.FINAL
    assert policy_port.tenant_ids == [message.tenant_id]

    class _NoneTool:
        def execute(self, proposal_value: AgentProposal, tool_call: ToolCall) -> ToolExecutionResult:
            del proposal_value, tool_call
            return ToolExecutionResult(data=None)

    none_result = AgentLoop(
        _SequenceLLM([proposal("find_customer"), proposal(None)]),
        _NoneTool(),
    ).run(initial_messages)
    tool_messages = [
        item for item in none_result.messages if item.role is MessageRole.TOOL
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_result is None

    repeated_result = AgentLoop(
        _SequenceLLM([proposal("find_customer")] * 3),
        _NoneTool(),
    ).run(initial_messages)
    assert repeated_result.reason is StopReason.REPEATED_TOOL
    assert repeated_result.steps == 3

    assert MAX_STEPS == 8
    assert AgentLoop(_SequenceLLM([proposal(None)]), _NoneTool()).max_steps == MAX_STEPS


def test_webhook_endpoint_accepts_body_only_message_and_uses_authenticated_tenant(
    inbound_client: tuple[TestClient, Tenant, _RecordingInboundService],
) -> None:
    client, tenant, service = inbound_client

    response = client.post("/v1/inbound/email/webhook", json=_payload())

    assert response.status_code == 202
    assert response.json() == {"job_id": str(service.job_id), "status": "queued"}
    assert len(service.messages) == 1
    message = service.messages[0]
    assert message.tenant_id == tenant.id
    assert message.tenant_slug == tenant.slug
    assert message.provider_id == "provider-123"
    assert message.attachments == ()


def test_inbound_webhook_enqueue_uses_a_fresh_db_session_after_auth_lookup() -> None:
    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == "/v1/inbound/email/webhook"
    )
    auth_dependency = next(
        dependency
        for dependency in route.dependant.dependencies
        if dependency.call is get_current_inbound_tenant
    )
    service_dependency = next(
        dependency
        for dependency in route.dependant.dependencies
        if dependency.call is get_inbound_intake_service
    )
    auth_session = next(
        dependency
        for dependency in auth_dependency.dependencies
        if dependency.call is get_db_session
    )
    service_session = next(
        dependency
        for dependency in service_dependency.dependencies
        if dependency.call is get_db_session
    )

    assert auth_session.use_cache is True
    assert service_session.use_cache is False


def test_webhook_endpoint_accepts_bounded_attachment_and_passes_decoded_bytes_to_service(
    inbound_client: tuple[TestClient, Tenant, _RecordingInboundService],
) -> None:
    client, _, service = inbound_client
    raw_pdf = b"%PDF-attachment"

    response = client.post(
        "/v1/inbound/email/webhook",
        json=_payload(attachments=[_attachment(content=raw_pdf)]),
    )

    assert response.status_code == 202
    assert len(service.messages) == 1
    assert service.messages[0].attachments[0].content == raw_pdf
    assert service.messages[0].attachments[0].text is None


def test_webhook_endpoint_rejects_payload_tenant_spoof_without_calling_service(
    inbound_client: tuple[TestClient, Tenant, _RecordingInboundService],
) -> None:
    client, _, service = inbound_client

    response = client.post(
        "/v1/inbound/email/webhook",
        json=_payload(tenant_id=str(uuid4())),
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid inbound payload"}
    assert service.messages == []


def test_webhook_endpoint_rejects_non_json_content_type(
    inbound_client: tuple[TestClient, Tenant, _RecordingInboundService],
) -> None:
    client, _, service = inbound_client

    response = client.post(
        "/v1/inbound/email/webhook",
        content=json.dumps(_payload()).encode("utf-8"),
        headers={"content-type": "text/plain"},
    )

    assert response.status_code == 415
    assert response.json() == {"detail": "Unsupported media type"}
    assert service.messages == []


def test_webhook_endpoint_sanitizes_malformed_payload(
    inbound_client: tuple[TestClient, Tenant, _RecordingInboundService],
) -> None:
    client, _, service = inbound_client

    response = client.post(
        "/v1/inbound/email/webhook",
        content=b"{not-json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid inbound payload"}
    assert service.messages == []


def test_webhook_endpoint_rejects_oversized_raw_body_before_parsing(
    inbound_client: tuple[TestClient, Tenant, _RecordingInboundService],
) -> None:
    client, _, service = inbound_client

    response = client.post(
        "/v1/inbound/email/webhook",
        content=b"x" * (MAX_INBOUND_WEBHOOK_BODY_BYTES + 1),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Inbound webhook body too large"}
    assert service.messages == []


def test_webhook_endpoint_rejects_chunked_body_over_limit_before_processing() -> None:
    service_requests: list[None] = []
    service = _RecordingInboundService()

    def override_inbound_service() -> _RecordingInboundService:
        service_requests.append(None)
        return service

    class _NoDbUse:
        async def execute(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("database lookup must not run for an oversized body")

    async def override_session():
        yield _NoDbUse()

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_inbound_intake_service] = override_inbound_service
    app.dependency_overrides[get_db_session] = override_session
    try:
        def chunked_body():
            yield b"{" + b"x" * (MAX_INBOUND_WEBHOOK_BODY_BYTES // 2)
            yield b"y" * (MAX_INBOUND_WEBHOOK_BODY_BYTES // 2 + 1) + b"}"

        with TestClient(app) as client:
            response = client.post(
                "/v1/inbound/email/webhook",
                content=chunked_body(),
                headers={
                    "content-type": "application/json",
                    "x-api-key": "ik_" + "A" * 43,
                },
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)

    assert response.status_code == 413
    assert response.json() == {"detail": "Inbound webhook body too large"}
    assert response.request.headers["transfer-encoding"] == "chunked"
    assert service_requests == []
    assert service.messages == []


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (
            IdempotencyConflict("provider-id-conflict-secret"),
            409,
            "Idempotency-Key conflicts with request",
        ),
        (
            TenantProfileUnavailableError("profile-path-secret"),
            503,
            "Tenant profile unavailable",
        ),
        (ValueError("internal-value-secret"), 422, "Invalid inbound payload"),
    ],
)
def test_webhook_endpoint_sanitizes_enqueue_failures(
    inbound_client: tuple[TestClient, Tenant, _RecordingInboundService],
    error: Exception,
    status_code: int,
    detail: str,
) -> None:
    client, _, service = inbound_client
    service.error = error

    response = client.post("/v1/inbound/email/webhook", json=_payload())

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    assert str(error) not in response.text


def test_legacy_intake_route_keeps_its_existing_missing_idempotency_contract(
    inbound_client: tuple[TestClient, Tenant, _RecordingInboundService],
) -> None:
    client, _, _ = inbound_client

    response = client.post(
        "/v1/intake",
        headers={"X-API-Key": "ik_legacy-test"},
        json={"channel": "email", "subject": "Load", "body": "Need a truck"},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Idempotency-Key is required"}
