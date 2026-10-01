"""Run one opt-in synthetic body intake against a local running stack."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable, Mapping
from uuid import uuid4

import httpx


PROFILE_SCENARIOS: dict[str, tuple[str, dict[str, str]]] = {
    "freight-broker": (
        "freight_broker",
        {
            "channel": "email",
            "subject": "Synthetic freight request",
            "body": (
                "Origin: Chicago; destination: Detroit; equipment: dry_van; "
                "pickup_window: 2026-10-01T09:00:00Z; commodity: appliances; "
                "contact: ops@example.test"
            ),
        },
    ),
    "repair-service": (
        "repair_service",
        {
            "channel": "web",
            "subject": "Synthetic repair request",
            "body": (
                "Customer: Alex Example; contact: alex@example.test; issue: "
                "the appliance will not start; asset_type: appliance"
            ),
        },
    ),
    "language-school": (
        "language_school",
        {
            "channel": "web",
            "subject": "Synthetic course inquiry",
            "body": (
                "Student: Alex Example; contact: alex@example.test; "
                "language: english"
            ),
        },
    ),
}
TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "failed_uncertain", "awaiting_approval"}
)
SAFE_JOB_RESPONSE_KEYS = frozenset(
    {
        "job_id",
        "trace_id",
        "scenario_key",
        "status",
        "attempt_count",
        "routing_status",
        "routing_reason",
        "missing_required_fields",
        "approval",
        "error_code",
        "created_at",
        "started_at",
        "finished_at",
    }
)


class SmokeFailure(RuntimeError):
    """Stable, source-free failure for the local smoke command."""

    def __init__(self, code: str, *, last_state: Mapping[str, object] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.last_state = dict(last_state) if last_state is not None else None


def _safe_job_response(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not set(value).issubset(SAFE_JOB_RESPONSE_KEYS):
        raise SmokeFailure("unsafe_job_response")
    required = {"job_id", "trace_id", "scenario_key", "status", "attempt_count"}
    if not required.issubset(value):
        raise SmokeFailure("malformed_job_response")
    if not all(isinstance(value[key], str) for key in ("job_id", "trace_id", "scenario_key", "status")):
        raise SmokeFailure("malformed_job_response")
    if value["status"] not in {"queued", "running", *TERMINAL_STATUSES}:
        raise SmokeFailure("malformed_job_response")
    if not isinstance(value["attempt_count"], int) or isinstance(value["attempt_count"], bool):
        raise SmokeFailure("malformed_job_response")
    missing = value.get("missing_required_fields", [])
    if not isinstance(missing, list) or not all(isinstance(item, str) for item in missing):
        raise SmokeFailure("malformed_job_response")
    approval = value.get("approval")
    if approval is not None and (
        not isinstance(approval, dict)
        or not set(approval).issubset({"approval_id", "status"})
        or not isinstance(approval.get("approval_id"), str)
        or not isinstance(approval.get("status"), str)
    ):
        raise SmokeFailure("unsafe_job_response")
    return dict(value)


def _json_response(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError as error:
        raise SmokeFailure("malformed_provider_response") from error


def run_smoke(
    *,
    profile: str,
    base_url: str,
    api_key: str,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 1.0,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Submit one synthetic intake and return only the safe terminal projection."""

    if not api_key.strip():
        raise SmokeFailure("tenant_api_key_missing")
    if profile not in PROFILE_SCENARIOS:
        raise SmokeFailure("profile_invalid")
    if (
        not math.isfinite(timeout_seconds)
        or not math.isfinite(poll_interval_seconds)
        or timeout_seconds <= 0
        or poll_interval_seconds < 0
    ):
        raise SmokeFailure("poll_bounds_invalid")

    expected_scenario, payload = PROFILE_SCENARIOS[profile]
    owned_client = client is None
    http_client = client or httpx.Client(timeout=10.0)
    headers = {
        "X-API-Key": api_key,
        "Idempotency-Key": f"demo-{uuid4()}",
        "Content-Type": "application/json",
    }
    try:
        try:
            accepted = http_client.post(
                f"{base_url.rstrip('/')}/v1/intake",
                headers=headers,
                json=payload,
            )
        except httpx.RequestError as error:
            raise SmokeFailure("intake_network_error") from error
        if accepted.status_code != 202:
            if accepted.status_code == 401:
                raise SmokeFailure("intake_unauthorized")
            raise SmokeFailure(f"intake_http_{accepted.status_code}")
        accepted_payload = _json_response(accepted)
        if not isinstance(accepted_payload, dict):
            raise SmokeFailure("malformed_intake_response")
        job_id = accepted_payload.get("job_id")
        if not isinstance(job_id, str) or accepted_payload.get("status") != "queued":
            raise SmokeFailure("malformed_intake_response")
        trace_id = accepted.headers.get("X-Trace-ID")
        if not trace_id:
            raise SmokeFailure("trace_id_missing")

        deadline = clock() + timeout_seconds
        poll_count = 0
        max_polls = max(1, min(10_000, int(timeout_seconds * 1_000) + 1))
        last_state: dict[str, object] | None = None
        while clock() < deadline and poll_count < max_polls:
            try:
                response = http_client.get(
                    f"{base_url.rstrip('/')}/v1/jobs/{job_id}",
                    headers={"X-API-Key": api_key},
                )
            except httpx.RequestError as error:
                raise SmokeFailure("job_network_error") from error
            if response.status_code == 401:
                raise SmokeFailure("job_unauthorized")
            if response.status_code != 200:
                raise SmokeFailure(f"job_http_{response.status_code}")
            job = _safe_job_response(_json_response(response))
            last_state = job
            if job["job_id"] != job_id or job["trace_id"] != trace_id:
                raise SmokeFailure("trace_id_mismatch")
            if job["scenario_key"] != expected_scenario:
                raise SmokeFailure("scenario_mismatch")
            if job["status"] in TERMINAL_STATUSES:
                if job["status"] in {"succeeded", "awaiting_approval"}:
                    return job
                raise SmokeFailure(f"job_{job['status']}", last_state=job)
            poll_count += 1
            sleep(poll_interval_seconds)
        raise SmokeFailure("poll_timeout", last_state=last_state)
    finally:
        if owned_client:
            http_client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(PROFILE_SCENARIOS), default="freight-broker")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    arguments = parser.parse_args(argv)
    api_key = os.environ.get("INTEGRA_DEMO_API_KEY", "")
    try:
        result = run_smoke(
            profile=arguments.profile,
            base_url=arguments.base_url,
            api_key=api_key,
            timeout_seconds=arguments.timeout_seconds,
            poll_interval_seconds=arguments.poll_interval_seconds,
        )
    except SmokeFailure as error:
        output: dict[str, object] = {"error_code": error.code}
        if error.last_state is not None:
            output["last_state"] = error.last_state
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
