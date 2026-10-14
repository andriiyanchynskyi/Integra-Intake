from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from app.agent import ResultReference
from app.auth import verify_inbound_webhook_signature
from app.domain.jobs import JobRead
import scripts.live_provider_smoke as smoke
from scripts.live_provider_smoke import (
    PROFILE_SCENARIOS,
    SAFE_JOB_RESPONSE_KEYS,
    SmokeFailure,
    render_human,
    run_custom_input,
    run_demo_case,
    run_webhook_idempotency,
    run_webhook_smoke,
    run_smoke,
)


RAW_TENANT_KEY = "ik_" + "a" * 43
TRACE_ID = "9b5d4f93-71c0-47ae-9b07-4d46b8e51f18"
JOB_ID = "189a0f9e-1d4c-42b5-9f4f-5df3f2b7d4f4"
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
CASE_ID = UUID("5f83b8cb-8b9c-4abf-999c-91b91aaf95d2")


@dataclass
class SequenceTransport:
    responses: list[httpx.Response]

    def __post_init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        response.request = request
        return response


def _accepted() -> httpx.Response:
    return httpx.Response(
        202,
        json={"job_id": JOB_ID, "status": "queued"},
        headers={"X-Trace-ID": TRACE_ID},
    )


def _job_payload(
    status: str,
    *,
    scenario_key: str | None = "freight_broker",
    result_references: tuple[ResultReference, ...] = (),
) -> dict[str, object]:
    return JobRead(
        job_id=UUID(JOB_ID),
        trace_id=UUID(TRACE_ID),
        scenario_key=scenario_key,
        status=status,
        attempt_count=1,
        agent_steps=1 if status in {"succeeded", "awaiting_approval"} else None,
        stop_reason=(
            "executor_stopped"
            if status in {"succeeded", "awaiting_approval"}
            else None
        ),
        execution_path=(),
        routing_status="ready" if status == "succeeded" else None,
        routing_reason="no_action_requested" if status == "succeeded" else None,
        missing_required_fields=(),
        approval=None,
        result_references=result_references,
        error_code=None,
        created_at=NOW,
        started_at=NOW if status != "queued" else None,
        finished_at=NOW if status == "succeeded" else None,
    ).model_dump(mode="json")


def _missing_contact_job_payload() -> dict[str, object]:
    payload = _job_payload("succeeded")
    payload.update(
        routing_status="awaiting_input",
        routing_reason="missing_required_fields",
        missing_required_fields=["contact"],
        approval=None,
        execution_path=[
            {
                "step": 1,
                "action_key": "create_case",
                "action_known": True,
                "policy_decision": "deny",
                "routing_status": "awaiting_input",
                "policy_reason": "missing_required_fields",
                "missing_required_fields": ["contact"],
                "tool_outcome": None,
                "side_effect_committed": False,
            }
        ],
    )
    return payload


def _job(
    status: str,
    *,
    scenario_key: str | None = "freight_broker",
    result_references: tuple[ResultReference, ...] = (),
) -> httpx.Response:
    return httpx.Response(
        200,
        json=_job_payload(
            status,
            scenario_key=scenario_key,
            result_references=result_references,
        ),
    )


def _run_for_transport(
    transport: str,
    *,
    client: httpx.Client,
    clock: Callable[[], float] = lambda: 1.0,
) -> dict[str, object]:
    common = {
        "profile": "freight-broker",
        "base_url": "http://testserver",
        "api_key": RAW_TENANT_KEY,
        "timeout_seconds": 5,
        "poll_interval_seconds": 0,
        "client": client,
        "clock": clock,
        "sleep": lambda _: None,
    }
    if transport == "webhook":
        return run_webhook_smoke(timestamp=1_800_000_000, **common)
    return run_smoke(**common)


def test_run_smoke_posts_safe_body_and_polls_to_success() -> None:
    transport = SequenceTransport([_accepted(), _job("succeeded")])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        result = run_smoke(
            profile="freight-broker",
            base_url="http://testserver",
            api_key=RAW_TENANT_KEY,
            timeout_seconds=5,
            poll_interval_seconds=0,
            client=client,
            clock=lambda: 1.0,
            sleep=lambda _: None,
        )
    finally:
        client.close()

    assert result["status"] == "succeeded"
    assert result["scenario_key"] == "freight_broker"
    assert set(result) <= SAFE_JOB_RESPONSE_KEYS
    assert len(transport.requests) == 2
    intake = json.loads(transport.requests[0].content)
    assert set(intake) == {"channel", "subject", "body"}
    assert "freight-broker" not in json.dumps(intake)
    assert transport.requests[0].headers["X-API-Key"] == RAW_TENANT_KEY
    assert transport.requests[0].headers["Idempotency-Key"].startswith("demo-")


def test_run_webhook_smoke_posts_signed_attachment_and_polls(tmp_path) -> None:
    attachment = tmp_path / "fixture.txt"
    attachment.write_text("Origin: Chicago", encoding="utf-8")
    transport = SequenceTransport([_accepted(), _job("succeeded")])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        result = run_webhook_smoke(
            profile="freight-broker",
            base_url="http://testserver",
            api_key=RAW_TENANT_KEY,
            subject="Synthetic webhook",
            body="Attached document",
            provider_id="provider-live-1",
            attachment=attachment,
            timestamp=1_800_000_000,
            timeout_seconds=5,
            poll_interval_seconds=0,
            client=client,
            clock=lambda: 1.0,
            sleep=lambda _: None,
        )
    finally:
        client.close()

    assert result["status"] == "succeeded"
    request = transport.requests[0]
    assert request.url.path == "/v1/inbound/email/webhook"
    assert request.headers["X-API-Key"] == RAW_TENANT_KEY
    assert request.headers["X-Inbound-Timestamp"] == "1800000000"
    assert verify_inbound_webhook_signature(
        RAW_TENANT_KEY,
        "1800000000",
        request.headers["X-Inbound-Signature"],
        request.content,
        now=1_800_000_000,
    )
    payload = json.loads(request.content)
    assert payload["provider_id"] == "provider-live-1"
    assert payload["attachments"][0]["media_type"] == "text/plain"
    assert payload["attachments"][0]["content_base64"]
    assert "tenant_id" not in payload
    assert len(transport.requests) == 2


def test_run_webhook_smoke_posts_pdf_and_malformed_fixtures(tmp_path) -> None:
    attachment = tmp_path / "fixture.pdf"
    attachment.write_bytes(b"%PDF-synthetic")
    transport = SequenceTransport([_accepted(), _job("succeeded")])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        run_webhook_smoke(
            profile="freight-broker",
            base_url="http://testserver",
            api_key=RAW_TENANT_KEY,
            attachment=attachment,
            timestamp=1_800_000_000,
            timeout_seconds=5,
            poll_interval_seconds=0,
            client=client,
            clock=lambda: 1.0,
            sleep=lambda _: None,
        )
    finally:
        client.close()

    payload = json.loads(transport.requests[0].content)
    assert payload["attachments"][0]["media_type"] == "application/pdf"


def test_run_webhook_duplicate_reuses_job_identity() -> None:
    transport = SequenceTransport([_accepted(), _accepted(), _job("succeeded")])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        result, duplicate_reused, conflict_rejected = run_webhook_idempotency(
            profile="freight-broker",
            base_url="http://testserver",
            api_key=RAW_TENANT_KEY,
            provider_id="provider-duplicate",
            conflict=False,
            timestamp=1_800_000_000,
            timeout_seconds=5,
            poll_interval_seconds=0,
            client=client,
            clock=lambda: 1.0,
            sleep=lambda _: None,
        )
    finally:
        client.close()

    assert result["status"] == "succeeded"
    assert duplicate_reused is True
    assert conflict_rejected is False
    assert len(transport.requests) == 3


def test_run_webhook_conflict_requires_409_without_echoing_body() -> None:
    transport = SequenceTransport(
        [
            _accepted(),
            httpx.Response(409, json={"detail": "secret source body"}),
            _job("succeeded"),
        ]
    )
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        result, duplicate_reused, conflict_rejected = run_webhook_idempotency(
            profile="freight-broker",
            base_url="http://testserver",
            api_key=RAW_TENANT_KEY,
            provider_id="provider-conflict",
            conflict=True,
            timestamp=1_800_000_000,
            timeout_seconds=5,
            poll_interval_seconds=0,
            client=client,
            clock=lambda: 1.0,
            sleep=lambda _: None,
        )
    finally:
        client.close()

    assert result["status"] == "succeeded"
    assert duplicate_reused is False
    assert conflict_rejected is True


def test_custom_webhook_input_file_resolves_relative_attachment(tmp_path) -> None:
    attachment = tmp_path / "fixture.txt"
    attachment.write_text("Synthetic attachment", encoding="utf-8")
    input_file = tmp_path / "request.json"
    input_file.write_text(
        json.dumps(
            {
                "transport": "webhook",
                "provider_id": "custom-message-1",
                "from_addr": "dispatcher@example.test",
                "subject": "Custom webhook",
                "body": "Custom body",
                "attachment_path": "fixture.txt",
            }
        ),
        encoding="utf-8",
    )
    transport = SequenceTransport([_accepted(), _job("succeeded")])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        report = run_custom_input(
            input_file=input_file,
            profile="freight-broker",
            base_url="http://testserver",
            api_key=RAW_TENANT_KEY,
            client=client,
            timeout_seconds=5,
            poll_interval_seconds=0,
            clock=lambda: 1.0,
            sleep=lambda _: None,
        )
    finally:
        client.close()

    assert report["transport"] == "webhook"
    assert report["input_kind"] == "text attachment"
    assert json.loads(transport.requests[0].content)["attachments"]


@pytest.mark.parametrize(
    "payload",
    [
        {"transport": "intake", "channel": "email", "subject": "x", "body": "y", "extra": "x"},
        {"transport": "webhook", "provider_id": "x", "from_addr": "x", "subject": "x", "body": "y", "attachment_path": "../secret.pdf"},
    ],
)
def test_custom_input_file_rejects_unsafe_schema_or_attachment_path(tmp_path, payload) -> None:
    input_file = tmp_path / "request.json"
    input_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SmokeFailure, match="input_file_invalid|attachment_path_invalid"):
        smoke._load_custom_input(input_file)


def test_custom_input_file_rejects_absolute_attachment_path(tmp_path) -> None:
    attachment = tmp_path / "fixture.txt"
    attachment.write_text("Synthetic attachment", encoding="utf-8")
    input_file = tmp_path / "request.json"
    input_file.write_text(
        json.dumps(
            {
                "transport": "webhook",
                "provider_id": "custom-message-absolute",
                "from_addr": "dispatcher@example.test",
                "subject": "Custom webhook",
                "body": "Custom body",
                "attachment_path": str(attachment),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SmokeFailure, match="attachment_path_invalid"):
        smoke._load_custom_input(input_file)


def test_safe_job_response_validates_execution_path() -> None:
    response = _job("succeeded")
    payload = response.json()
    payload["agent_steps"] = 1
    payload["stop_reason"] = "executor_stopped"
    payload["execution_path"] = [
        {
            "step": 1,
            "action_key": "create_case",
            "action_known": True,
            "policy_decision": "allow",
            "routing_status": "ready",
            "policy_reason": "action_allowed",
            "missing_required_fields": [],
            "tool_outcome": "executed",
            "side_effect_committed": True,
        }
    ]

    safe = smoke._safe_job_response(payload)

    assert safe["execution_path"][0]["action_key"] == "create_case"


def test_safe_job_response_rejects_unsafe_execution_identifier() -> None:
    payload = _job("succeeded").json()
    payload["execution_path"] = [
        {
            "step": 1,
            "action_key": "CreateCase",
            "action_known": True,
            "policy_decision": "allow",
            "routing_status": "ready",
            "policy_reason": "action_allowed",
            "missing_required_fields": [],
            "tool_outcome": "executed",
            "side_effect_committed": True,
        }
    ]

    with pytest.raises(SmokeFailure, match="malformed_job_response"):
        smoke._safe_job_response(payload)


def test_safe_job_response_accepts_complete_serialized_job_read() -> None:
    payload = _job_payload(
        "succeeded",
        result_references=(ResultReference(kind="case", id=CASE_ID),),
    )

    safe = smoke._safe_job_response(payload)

    assert set(safe) == set(JobRead.model_fields)
    assert safe["result_references"] == [
        {"kind": "case", "id": str(CASE_ID)}
    ]


@pytest.mark.parametrize(
    "reason",
    ["no_action_requested", "invalid_field_values", "action_not_executable"],
)
def test_safe_job_response_accepts_current_policy_reasons(reason: str) -> None:
    payload = _job_payload("succeeded")
    payload["execution_path"] = [
        {
            "step": 1,
            "action_key": None,
            "action_known": False,
            "policy_decision": "allow" if reason == "no_action_requested" else "deny",
            "routing_status": (
                "ready" if reason == "no_action_requested" else "awaiting_input"
            ),
            "policy_reason": reason,
            "missing_required_fields": [],
            "tool_outcome": None,
            "side_effect_committed": False,
        }
    ]

    assert smoke._safe_job_response(payload)["execution_path"][0][
        "policy_reason"
    ] == reason


@pytest.mark.parametrize(
    "references",
    [
        [{"kind": "unknown", "id": str(CASE_ID)}],
        [{"kind": "case", "id": "not-a-uuid"}],
        [
            {"kind": "case", "id": str(CASE_ID)},
            {"kind": "case", "id": str(UUID(int=2))},
        ],
        [
            {"kind": "case", "id": str(UUID(int=1))},
            {"kind": "reply_draft", "id": str(UUID(int=2))},
            {"kind": "review_flag", "id": str(UUID(int=3))},
            {"kind": "case", "id": str(UUID(int=4))},
        ],
    ],
)
def test_safe_job_response_rejects_invalid_result_references(
    references: list[dict[str, str]],
) -> None:
    payload = _job_payload("succeeded")
    payload["result_references"] = references

    with pytest.raises(SmokeFailure, match="malformed_job_response"):
        smoke._safe_job_response(payload)


def test_failed_job_parser_accepts_null_scenario() -> None:
    safe = smoke._safe_job_response(
        _job_payload("failed", scenario_key=None)
    )

    assert safe["scenario_key"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_id", UUID(JOB_ID)),
        ("job_id", "not-a-uuid"),
        ("trace_id", UUID(TRACE_ID)),
        ("trace_id", "not-a-uuid"),
        ("created_at", 1_760_443_200),
        ("created_at", "2026-10-14T12:00:00"),
        ("started_at", "not-a-timestamp"),
        ("routing_status", "unknown"),
        ("routing_reason", "unknown"),
        ("error_code", "unknown"),
        ("missing_required_fields", ["Unsafe-Field"]),
    ],
)
def test_safe_job_response_rejects_invalid_public_field_values(
    field: str,
    value: object,
) -> None:
    payload = _job_payload("succeeded")
    payload[field] = value

    with pytest.raises(SmokeFailure, match="malformed_job_response"):
        smoke._safe_job_response(payload)


@pytest.mark.parametrize(
    "approval",
    [
        {"approval_id": "not-a-uuid", "status": "pending"},
        {"approval_id": str(UUID(int=1)), "status": "unknown"},
        {"approval_id": UUID(int=1), "status": "pending"},
    ],
)
def test_safe_job_response_rejects_invalid_approval_values(
    approval: dict[str, object],
) -> None:
    payload = _job_payload("awaiting_approval")
    payload["approval"] = approval

    with pytest.raises(SmokeFailure, match="malformed_job_response"):
        smoke._safe_job_response(payload)


def test_safe_job_response_rejects_unknown_execution_policy_reason() -> None:
    payload = _job_payload("succeeded")
    payload["execution_path"] = [
        {
            "step": 1,
            "action_key": None,
            "action_known": False,
            "policy_decision": "deny",
            "routing_status": "rejected",
            "policy_reason": "unknown",
            "missing_required_fields": [],
            "tool_outcome": None,
            "side_effect_committed": False,
        }
    ]

    with pytest.raises(SmokeFailure, match="malformed_job_response"):
        smoke._safe_job_response(payload)


@pytest.mark.parametrize(
    "status", ["queued", "running", "succeeded", "awaiting_approval"]
)
def test_polling_rejects_null_scenario_for_non_failed_job(status: str) -> None:
    transport = SequenceTransport([_job(status, scenario_key=None)])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure, match="scenario_mismatch"):
            smoke._poll_job(
                http_client=client,
                base_url="http://testserver",
                api_key=RAW_TENANT_KEY,
                progress=smoke.SmokeProgress.accepted(JOB_ID, TRACE_ID),
                expected_scenario="freight_broker",
                timeout_seconds=5,
                poll_interval_seconds=0,
                clock=lambda: 1.0,
                sleep=lambda _: None,
            )
    finally:
        client.close()


def test_expect_path_failure_uses_stable_error_code() -> None:
    transport = SequenceTransport([_accepted(), _job("succeeded")])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure, match="expected_path_not_observed"):
            run_demo_case(
                case="body-complete",
                profile="freight-broker",
                base_url="http://testserver",
                api_key=RAW_TENANT_KEY,
                expect_path="tool",
                client=client,
                timeout_seconds=5,
                poll_interval_seconds=0,
                clock=lambda: 1.0,
                sleep=lambda _: None,
            )
    finally:
        client.close()


def test_named_body_demo_case_returns_safe_report() -> None:
    transport = SequenceTransport([_accepted(), _job("succeeded")])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        report = run_demo_case(
            case="body-complete",
            profile="freight-broker",
            base_url="http://testserver",
            api_key=RAW_TENANT_KEY,
            client=client,
            timeout_seconds=5,
            poll_interval_seconds=0,
            clock=lambda: 1.0,
            sleep=lambda _: None,
        )
    finally:
        client.close()

    assert report["result"] == "PASS"
    assert report["demo_case"] == "body-complete"
    assert report["endpoint"] == "/v1/intake"


def test_every_named_demo_case_has_bounded_explanation_metadata() -> None:
    assert set(smoke.DEMO_CASE_EXPLANATIONS) == set(smoke.DemoCase)
    for explanation in smoke.DEMO_CASE_EXPLANATIONS.values():
        assert explanation.purpose.strip()
        assert explanation.expected_outcome.strip()
        assert len(explanation.purpose) <= 300
        assert len(explanation.expected_outcome) <= 500


def test_named_demo_report_contains_source_free_explanations() -> None:
    transport = SequenceTransport([_accepted(), _job("succeeded")])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        report = run_demo_case(
            case="body-complete",
            profile="freight-broker",
            base_url="http://testserver",
            api_key=RAW_TENANT_KEY,
            client=client,
            timeout_seconds=5,
            poll_interval_seconds=0,
            clock=lambda: 1.0,
            sleep=lambda _: None,
        )
    finally:
        client.close()

    assert report["purpose"] == smoke.DEMO_CASE_EXPLANATIONS[
        smoke.DemoCase.BODY_COMPLETE
    ].purpose
    assert report["expected_outcome"] == smoke.DEMO_CASE_EXPLANATIONS[
        smoke.DemoCase.BODY_COMPLETE
    ].expected_outcome
    assert "job status=succeeded" in report["observed_outcome"]
    assert "SOURCE_SECRET" not in json.dumps(report)


def test_default_and_custom_modes_describe_connectivity_not_fixed_semantics(
    tmp_path,
    monkeypatch,
) -> None:
    job = smoke._safe_job_response(_job_payload("succeeded"))
    monkeypatch.setattr(smoke, "run_smoke", lambda **_: job)

    default_report = smoke.run_live_report(
        profile="freight-broker",
        base_url="http://testserver",
        api_key=RAW_TENANT_KEY,
    )

    input_file = tmp_path / "custom.json"
    input_file.write_text(
        json.dumps(
            {
                "transport": "intake",
                "channel": "email",
                "subject": "Synthetic custom request",
                "body": "Synthetic custom body",
            }
        ),
        encoding="utf-8",
    )
    custom_report = smoke.run_live_report(
        profile="freight-broker",
        base_url="http://testserver",
        api_key=RAW_TENANT_KEY,
        input_file=input_file,
    )

    assert "connectivity" in default_report["purpose"].lower()
    assert "safe terminal" in default_report["expected_outcome"].lower()
    assert "caller-supplied synthetic input" in custom_report["purpose"]
    assert "fixed business semantics" in custom_report["expected_outcome"]


def test_body_complete_awaiting_approval_requires_policy_path() -> None:
    pending_payload = _job("awaiting_approval").json()
    pending_payload["approval"] = {
        "approval_id": "3a0d3ec4-1a16-4f4f-a4c8-1efbb7b8d45a",
        "status": "pending",
    }
    pending_payload["execution_path"] = [
        {
            "step": 1,
            "action_key": None,
            "action_known": False,
            "policy_decision": "needs_approval",
            "routing_status": "pending_approval",
            "policy_reason": "approval_required",
            "missing_required_fields": [],
            "tool_outcome": None,
            "side_effect_committed": False,
        }
    ]
    transport = SequenceTransport(
        [
            _accepted(),
            httpx.Response(200, json=pending_payload),
        ]
    )
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        report = run_demo_case(
            case="body-complete",
            profile="freight-broker",
            base_url="http://testserver",
            api_key=RAW_TENANT_KEY,
            client=client,
            timeout_seconds=5,
            poll_interval_seconds=0,
            clock=lambda: 1.0,
            sleep=lambda _: None,
        )
    finally:
        client.close()

    assert report["result"] == "PASS"
    assert report["job"]["status"] == "awaiting_approval"


def test_webhook_body_requires_exact_missing_contact_business_outcome() -> None:
    transport = SequenceTransport(
        [_accepted(), httpx.Response(200, json=_missing_contact_job_payload())]
    )
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        report = run_demo_case(
            case="webhook-body",
            profile="freight-broker",
            base_url="http://testserver",
            api_key=RAW_TENANT_KEY,
            client=client,
            timeout_seconds=5,
            poll_interval_seconds=0,
            clock=lambda: 1.0,
            sleep=lambda _: None,
        )
    finally:
        client.close()

    assert report["result"] == "PASS"
    assert {check["name"] for check in report["checks"]} >= {
        "status",
        "routing_status",
        "routing_reason",
        "missing_required_fields",
        "approval",
        "no_side_effect",
    }
    sent = json.loads(transport.requests[0].content)
    assert "contact:" not in sent["body"].lower()
    assert "origin:" in sent["body"].lower()
    assert "destination:" in sent["body"].lower()


def test_webhook_body_rejects_unrelated_safe_terminal_outcome() -> None:
    transport = SequenceTransport([_accepted(), _job("succeeded")])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure, match="expectation_failed"):
            run_demo_case(
                case="webhook-body",
                profile="freight-broker",
                base_url="http://testserver",
                api_key=RAW_TENANT_KEY,
                client=client,
                timeout_seconds=5,
                poll_interval_seconds=0,
                clock=lambda: 1.0,
                sleep=lambda _: None,
            )
    finally:
        client.close()


@pytest.mark.parametrize(
    ("field", "value", "failed_check"),
    [
        (
            "approval",
            {"approval_id": str(UUID(int=10)), "status": "pending"},
            "approval",
        ),
        (
            "execution_path",
            [
                {
                    "step": 1,
                    "action_key": "create_case",
                    "action_known": True,
                    "policy_decision": "deny",
                    "routing_status": "awaiting_input",
                    "policy_reason": "missing_required_fields",
                    "missing_required_fields": ["contact"],
                    "tool_outcome": "executed",
                    "side_effect_committed": True,
                }
            ],
            "no_side_effect",
        ),
    ],
)
def test_webhook_body_requires_no_approval_or_committed_side_effect(
    field: str,
    value: object,
    failed_check: str,
) -> None:
    job = _missing_contact_job_payload()
    job[field] = value

    checks = smoke._case_checks(smoke.DemoCase.WEBHOOK_BODY, job)

    assert next(check for check in checks if check["name"] == failed_check)[
        "passed"
    ] is False


def test_human_renderer_shows_safe_lifecycle_and_path_without_source() -> None:
    report = {
        "result": "PASS",
        "demo_case": "webhook-document-txt",
        "profile": "freight-broker",
        "transport": "webhook",
        "endpoint": "/v1/inbound/email/webhook",
        "input_kind": "text attachment",
        "purpose": "Verify signed text document intake.",
        "expected_outcome": "The job reaches a safe terminal status.",
        "observed_outcome": (
            "HTTP acceptance=202; job status=succeeded; routing status=ready; "
            "routing reason=action_allowed; missing fields=none; approval=none; "
            "committed side effect=yes."
        ),
        "acceptance_status": 202,
        "duplicate_reused": None,
        "conflict_rejected": None,
        "artifact_payload": "SOURCE_SECRET",
        "checks": [{"name": "terminal_status", "passed": True}],
        "job": {
            "job_id": JOB_ID,
            "trace_id": TRACE_ID,
            "scenario_key": "freight_broker",
            "status": "succeeded",
            "attempt_count": 1,
            "agent_steps": 1,
            "stop_reason": "executor_stopped",
            "routing_status": "ready",
            "routing_reason": "action_allowed",
            "missing_required_fields": [],
            "execution_path": [
                {
                    "step": 1,
                    "action_key": "create_case",
                    "action_known": True,
                    "policy_decision": "allow",
                    "routing_status": "ready",
                    "policy_reason": "action_allowed",
                    "missing_required_fields": [],
                    "tool_outcome": "executed",
                    "side_effect_committed": True,
                }
            ],
            "approval": None,
            "result_references": [
                {"kind": "case", "id": str(CASE_ID)},
                {"kind": "reply_draft", "id": str(UUID(int=5))},
            ],
            "error_code": None,
            "created_at": "2026-10-14T12:00:00Z",
            "started_at": "2026-10-14T12:00:00Z",
            "finished_at": "2026-10-14T12:00:00Z",
        },
    }

    rendered = render_human(report)

    assert "LIVE INTAKE SMOKE: PASS" in rendered
    assert rendered.index("Purpose:") < rendered.index("Job ID:")
    assert "Purpose:          Verify signed text document intake." in rendered
    assert "Expected:         The job reaches a safe terminal status." in rendered
    assert "Observed:         HTTP acceptance=202; job status=succeeded" in rendered
    assert "Trace ID:" in rendered
    assert "Execution path:" in rendered
    assert "Created at:       2026-10-14T12:00:00Z" in rendered
    assert "Started at:       2026-10-14T12:00:00Z" in rendered
    assert "Finished at:      2026-10-14T12:00:00Z" in rendered
    assert "Result references:" in rendered
    assert f"kind=case id={CASE_ID}" in rendered
    assert f"kind=reply_draft id={UUID(int=5)}" in rendered
    assert "action=create_case | known=True | policy=allow" in rendered
    assert "reason=action_allowed" in rendered
    assert "missing=NONE" in rendered
    assert "NOT OBSERVED" in rendered
    assert "SOURCE_SECRET" not in rendered
    assert RAW_TENANT_KEY not in rendered


def test_failure_report_keeps_mode_explanation_and_safe_progress() -> None:
    arguments = argparse.Namespace(
        transport=None,
        attachment=None,
        input_file=None,
        demo_case="webhook-body",
        profile="freight-broker",
        expect_path=None,
    )
    failure = SmokeFailure(
        "expectation_failed",
        progress=smoke.SmokeProgress.from_job(
            smoke._safe_job_response(_job_payload("succeeded"))
        ),
    )

    report = smoke._failure_report(arguments, failure)

    assert report["transport"] == "webhook"
    assert report["endpoint"] == "/v1/inbound/email/webhook"
    assert report["purpose"] == smoke.DEMO_CASE_EXPLANATIONS[
        smoke.DemoCase.WEBHOOK_BODY
    ].purpose
    assert "missing contact" in report["expected_outcome"].lower()
    assert "smoke error=expectation_failed" in report["observed_outcome"]
    assert "job status=succeeded" in report["observed_outcome"]


def test_json_cli_preserves_explanation_fields(monkeypatch, capsys) -> None:
    report = {
        "result": "PASS",
        "purpose": "Verify synthetic connectivity.",
        "expected_outcome": "The job reaches a safe terminal status.",
        "observed_outcome": "HTTP acceptance=202; job status=succeeded.",
        "job": _job_payload("succeeded"),
    }
    monkeypatch.setenv("INTEGRA_DEMO_API_KEY", RAW_TENANT_KEY)
    monkeypatch.setattr(smoke, "run_live_report", lambda **_: report)

    assert smoke.main(["--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["purpose"] == report["purpose"]
    assert output["expected_outcome"] == report["expected_outcome"]
    assert output["observed_outcome"] == report["observed_outcome"]


def test_human_renderer_distinguishes_empty_and_unobserved_result_references() -> None:
    empty = render_human({"job": {"result_references": []}})
    unobserved = render_human({"job": {}})

    assert "Result references:\n  NONE" in empty
    assert "Result references:\n  NOT OBSERVED" in unobserved


def test_json_output_contains_only_typed_result_reference_fields(
    monkeypatch,
    capsys,
) -> None:
    report = {
        "result": "PASS",
        "job": _job_payload(
            "succeeded",
            result_references=(ResultReference(kind="case", id=CASE_ID),),
        ),
    }
    monkeypatch.setenv("INTEGRA_DEMO_API_KEY", RAW_TENANT_KEY)
    monkeypatch.setattr(smoke, "run_live_report", lambda **_: report)

    assert smoke.main(["--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["job"]["result_references"] == [
        {"kind": "case", "id": str(CASE_ID)}
    ]
    assert set(output["job"]["result_references"][0]) == {"kind", "id"}
    assert "SOURCE_SECRET" not in json.dumps(output)


@pytest.mark.parametrize("profile", tuple(PROFILE_SCENARIOS))
def test_run_smoke_uses_each_profile_body_without_sending_profile_selector(profile: str) -> None:
    expected_scenario, expected_body = PROFILE_SCENARIOS[profile]
    transport = SequenceTransport([_accepted(), _job("succeeded", scenario_key=expected_scenario)])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        result = run_smoke(
            profile=profile,
            base_url="http://testserver",
            api_key=RAW_TENANT_KEY,
            timeout_seconds=5,
            poll_interval_seconds=0,
            client=client,
            clock=lambda: 1.0,
            sleep=lambda _: None,
        )
    finally:
        client.close()

    assert result["scenario_key"] == expected_scenario
    assert json.loads(transport.requests[0].content) == expected_body
    assert profile not in json.dumps(expected_body)


def test_run_smoke_accepts_awaiting_approval() -> None:
    transport = SequenceTransport([_accepted(), _job("awaiting_approval")])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        result = run_smoke(
            profile="freight-broker",
            base_url="http://testserver",
            api_key=RAW_TENANT_KEY,
            timeout_seconds=5,
            poll_interval_seconds=0,
            client=client,
            clock=lambda: 1.0,
            sleep=lambda _: None,
        )
    finally:
        client.close()

    assert result["status"] == "awaiting_approval"


@pytest.mark.parametrize("status", ["failed", "failed_uncertain"])
def test_run_smoke_reports_terminal_failure_without_response_body(status: str) -> None:
    transport = SequenceTransport([_accepted(), _job(status)])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure, match=f"job_{status}") as failure:
            run_smoke(
                profile="freight-broker",
                base_url="http://testserver",
                api_key=RAW_TENANT_KEY,
                timeout_seconds=5,
                poll_interval_seconds=0,
                client=client,
                clock=lambda: 1.0,
                sleep=lambda _: None,
            )
        assert failure.value.last_state is not None
        assert failure.value.last_state["status"] == status
    finally:
        client.close()


def test_failure_after_acceptance_preserves_safe_identity() -> None:
    transport = SequenceTransport(
        [_accepted(), httpx.Response(200, content=b"not-json SOURCE_SECRET")]
    )
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure) as failure:
            run_smoke(
                profile="freight-broker",
                base_url="http://testserver",
                api_key=RAW_TENANT_KEY,
                timeout_seconds=5,
                poll_interval_seconds=0,
                client=client,
                clock=lambda: 1.0,
                sleep=lambda _: None,
            )
    finally:
        client.close()

    arguments = argparse.Namespace(
        transport=None,
        attachment=None,
        input_file=None,
        demo_case=None,
        profile="freight-broker",
    )
    report = smoke._failure_report(arguments, failure.value)
    output = render_human(report)
    assert "HTTP accepted:    202" in output
    assert f"Job ID:           {JOB_ID}" in output
    assert f"Trace ID:         {TRACE_ID}" in output
    assert "Smoke error:      malformed_job_response" in output
    assert "not-json" not in output
    assert "SOURCE_SECRET" not in output


@pytest.mark.parametrize("transport_name", ["intake", "webhook"])
@pytest.mark.parametrize(
    ("poll_response", "expected_code"),
    [
        (httpx.Response(200, content=b"not-json SOURCE_SECRET"), "malformed_job_response"),
        (
            httpx.Response(
                200,
                json={**_job_payload("succeeded"), "SOURCE_SECRET": "source body"},
            ),
            "unsafe_job_response",
        ),
        (httpx.Response(401, json={"detail": "SOURCE_SECRET"}), "job_unauthorized"),
        (
            httpx.Response(
                200,
                json={**_job_payload("succeeded"), "trace_id": str(UUID(int=2))},
            ),
            "trace_id_mismatch",
        ),
        (
            httpx.Response(
                200,
                json={**_job_payload("succeeded"), "scenario_key": "repair_service"},
            ),
            "scenario_mismatch",
        ),
    ],
)
def test_post_acceptance_poll_failures_preserve_identity_without_payload(
    transport_name: str,
    poll_response: httpx.Response,
    expected_code: str,
) -> None:
    transport = SequenceTransport([_accepted(), poll_response])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure, match=expected_code) as failure:
            _run_for_transport(transport_name, client=client)
    finally:
        client.close()

    assert failure.value.progress.acceptance_status == 202
    assert failure.value.progress.job_id == JOB_ID
    assert failure.value.progress.trace_id == TRACE_ID
    arguments = argparse.Namespace(
        transport=transport_name,
        attachment=None,
        input_file=None,
        demo_case=None,
        profile="freight-broker",
    )
    rendered = render_human(smoke._failure_report(arguments, failure.value))
    assert "SOURCE_SECRET" not in rendered


@pytest.mark.parametrize("transport_name", ["intake", "webhook"])
def test_post_acceptance_network_failure_preserves_identity(
    transport_name: str,
) -> None:
    calls = 0

    def accept_then_fail(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _accepted()
        raise httpx.ConnectError("SOURCE_SECRET", request=request)

    client = httpx.Client(transport=httpx.MockTransport(accept_then_fail))
    try:
        with pytest.raises(SmokeFailure, match="job_network_error") as failure:
            _run_for_transport(transport_name, client=client)
    finally:
        client.close()

    assert failure.value.progress.acceptance_status == 202
    assert failure.value.progress.job_id == JOB_ID
    assert failure.value.progress.trace_id == TRACE_ID
    assert "SOURCE_SECRET" not in str(failure.value)


@pytest.mark.parametrize("transport_name", ["intake", "webhook"])
def test_post_acceptance_timeout_preserves_latest_validated_job(
    transport_name: str,
) -> None:
    transport = SequenceTransport([_accepted(), _job("running")])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure, match="poll_timeout") as failure:
            _run_for_transport(
                transport_name,
                client=client,
                clock=iter((0.0, 0.0, 6.0)).__next__,
            )
    finally:
        client.close()

    assert failure.value.progress.acceptance_status == 202
    assert failure.value.progress.last_state is not None
    assert failure.value.progress.last_state["status"] == "running"


def test_failed_job_with_null_scenario_keeps_safe_failure_state() -> None:
    payload = _job_payload("failed", scenario_key=None)
    payload["error_code"] = "snapshot_incompatible"
    transport = SequenceTransport([_accepted(), httpx.Response(200, json=payload)])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure, match="job_failed") as failure:
            run_smoke(
                profile="freight-broker",
                base_url="http://testserver",
                api_key=RAW_TENANT_KEY,
                client=client,
                timeout_seconds=5,
                poll_interval_seconds=0,
                clock=lambda: 1.0,
                sleep=lambda _: None,
            )
    finally:
        client.close()

    assert failure.value.progress.acceptance_status == 202
    assert failure.value.progress.last_state == smoke._safe_job_response(payload)


@pytest.mark.parametrize(
    ("transport_name", "expected_code"),
    [
        ("intake", "malformed_intake_response"),
        ("webhook", "malformed_webhook_response"),
    ],
)
def test_malformed_acceptance_json_uses_endpoint_specific_code(
    transport_name: str,
    expected_code: str,
) -> None:
    transport = SequenceTransport(
        [httpx.Response(202, content=b"not-json SOURCE_SECRET", headers={"X-Trace-ID": TRACE_ID})]
    )
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure, match=expected_code) as failure:
            _run_for_transport(transport_name, client=client)
    finally:
        client.close()

    assert "SOURCE_SECRET" not in str(failure.value)


def test_run_smoke_rejects_trace_or_scenario_mismatch() -> None:
    response = _job("succeeded")
    payload = response.json()
    payload["scenario_key"] = "repair_service"
    response = httpx.Response(200, json=payload)
    transport = SequenceTransport([_accepted(), response])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure, match="scenario_mismatch"):
            run_smoke(
                profile="freight-broker",
                base_url="http://testserver",
                api_key=RAW_TENANT_KEY,
                timeout_seconds=5,
                poll_interval_seconds=0,
                client=client,
                clock=lambda: 1.0,
                sleep=lambda _: None,
            )
    finally:
        client.close()


def test_run_smoke_times_out_without_unbounded_polling() -> None:
    transport = SequenceTransport([_accepted(), _job("running")])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure, match="poll_timeout") as failure:
            run_smoke(
                profile="freight-broker",
                base_url="http://testserver",
                api_key=RAW_TENANT_KEY,
                timeout_seconds=0.001,
                poll_interval_seconds=0,
                client=client,
                clock=iter((0.0, 0.0, 1.0)).__next__,
                sleep=lambda _: None,
            )
    finally:
        client.close()

    assert failure.value.last_state is not None
    assert failure.value.last_state["status"] == "running"


def test_run_smoke_rejects_missing_api_key_and_never_reads_provider_key() -> None:
    with pytest.raises(SmokeFailure, match="tenant_api_key_missing"):
        run_smoke(
            profile="freight-broker",
            base_url="http://testserver",
            api_key="",
            timeout_seconds=5,
            poll_interval_seconds=0,
        )


def test_run_smoke_reports_intake_authentication_failure() -> None:
    transport = SequenceTransport([httpx.Response(401, json={"detail": "secret"})])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure, match="intake_unauthorized"):
            run_smoke(
                profile="freight-broker",
                base_url="http://testserver",
                api_key=RAW_TENANT_KEY,
                client=client,
            )
    finally:
        client.close()


def test_run_smoke_reports_job_authentication_failure() -> None:
    transport = SequenceTransport([_accepted(), httpx.Response(401, json={"detail": "secret"})])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure, match="job_unauthorized"):
            run_smoke(
                profile="freight-broker",
                base_url="http://testserver",
                api_key=RAW_TENANT_KEY,
                timeout_seconds=5,
                poll_interval_seconds=0,
                client=client,
                clock=lambda: 1.0,
                sleep=lambda _: None,
            )
    finally:
        client.close()


def test_run_smoke_reports_network_failure_without_exception_text() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret provider response", request=request)

    client = httpx.Client(transport=httpx.MockTransport(fail))
    try:
        with pytest.raises(SmokeFailure, match="intake_network_error") as failure:
            run_smoke(
                profile="freight-broker",
                base_url="http://testserver",
                api_key=RAW_TENANT_KEY,
                client=client,
            )
    finally:
        client.close()

    assert "secret provider response" not in str(failure.value)


def test_cli_reads_only_demo_key_and_never_prints_credentials(monkeypatch, capsys) -> None:
    provider_key = "provider-secret"
    captured: dict[str, object] = {}

    monkeypatch.setenv("INTEGRA_DEMO_API_KEY", RAW_TENANT_KEY)
    monkeypatch.setenv("LLM_API_KEY", provider_key)

    def fake_run_smoke(**kwargs):
        captured.update(kwargs)
        return {
            "job_id": JOB_ID,
            "trace_id": TRACE_ID,
            "scenario_key": "repair_service",
            "status": "succeeded",
            "attempt_count": 1,
        }

    monkeypatch.setattr(smoke, "run_smoke", fake_run_smoke)

    assert smoke.main(["--profile", "repair-service"]) == 0
    output = capsys.readouterr().out
    assert captured["api_key"] == RAW_TENANT_KEY
    assert captured["profile"] == "repair-service"
    assert RAW_TENANT_KEY not in output
    assert provider_key not in output


def test_cli_failure_renders_validated_last_job_state(monkeypatch, capsys) -> None:
    last_state = _job("failed").json()
    last_state["error_code"] = "provider_request_rejected"

    def fail(**kwargs):
        raise SmokeFailure("job_failed", last_state=last_state)

    monkeypatch.setattr(smoke, "run_live_report", fail)

    assert smoke.main(["--profile", "freight-broker"]) == 1
    output = capsys.readouterr().out

    assert "LIVE INTAKE SMOKE: FAIL" in output
    assert "Job ID:" in output
    assert "Status:           failed" in output
    assert "Created at:       2026-10-14T12:00:00Z" in output
    assert "Error:            provider_request_rejected" in output
    assert "Smoke error:      job_failed" in output
