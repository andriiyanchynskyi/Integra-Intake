from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import pytest

from app.auth import verify_inbound_webhook_signature
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


def _job(status: str, *, scenario_key: str = "freight_broker") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "job_id": JOB_ID,
            "trace_id": TRACE_ID,
            "scenario_key": scenario_key,
            "status": status,
            "attempt_count": 1,
            "routing_status": "ready" if status == "succeeded" else None,
            "routing_reason": "complete" if status == "succeeded" else None,
            "missing_required_fields": [],
            "approval": None,
            "error_code": None,
            "created_at": "2026-09-30T12:00:00Z",
            "started_at": "2026-09-30T12:00:01Z",
            "finished_at": "2026-09-30T12:00:02Z",
        },
    )


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


def test_human_renderer_shows_safe_lifecycle_and_path_without_source() -> None:
    report = {
        "result": "PASS",
        "demo_case": "webhook-document-txt",
        "profile": "freight-broker",
        "transport": "webhook",
        "endpoint": "/v1/inbound/email/webhook",
        "input_kind": "text attachment",
        "acceptance_status": 202,
        "duplicate_reused": None,
        "conflict_rejected": None,
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
            "error_code": None,
            "created_at": "2026-09-30T12:00:00Z",
            "started_at": "2026-09-30T12:00:01Z",
            "finished_at": "2026-09-30T12:00:02Z",
        },
    }

    rendered = render_human(report)

    assert "LIVE INTAKE SMOKE: PASS" in rendered
    assert "Trace ID:" in rendered
    assert "Execution path:" in rendered
    assert "Created at:       2026-09-30T12:00:00Z" in rendered
    assert "Started at:       2026-09-30T12:00:01Z" in rendered
    assert "Finished at:      2026-09-30T12:00:02Z" in rendered
    assert "action=create_case | known=True | policy=allow" in rendered
    assert "reason=action_allowed" in rendered
    assert "missing=NONE" in rendered
    assert "NOT OBSERVED" in rendered
    assert "SOURCE_SECRET" not in rendered
    assert RAW_TENANT_KEY not in rendered


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
    assert "Created at:       2026-09-30T12:00:00Z" in output
    assert "Error:            provider_request_rejected" in output
    assert "Smoke error:      job_failed" in output
