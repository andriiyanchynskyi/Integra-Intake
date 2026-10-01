from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import pytest

import scripts.live_provider_smoke as smoke
from scripts.live_provider_smoke import (
    PROFILE_SCENARIOS,
    SAFE_JOB_RESPONSE_KEYS,
    SmokeFailure,
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
