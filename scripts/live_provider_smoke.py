"""Run one opt-in synthetic body intake against a local running stack."""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from app.auth import sign_inbound_webhook
from app.documents import MAX_DOCUMENT_BYTES
from app.tenants.identifiers import SAFE_IDENTIFIER_PATTERN


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
        "agent_steps",
        "stop_reason",
        "execution_path",
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
MAX_EXECUTION_STEPS = 8
WEBHOOK_DOCUMENT_MEDIA_TYPES = {
    ".txt": "text/plain",
    ".pdf": "application/pdf",
}
SAFE_IDENTIFIER_RE = re.compile(SAFE_IDENTIFIER_PATTERN)


class SmokeTransport(str, Enum):
    INTAKE = "intake"
    WEBHOOK = "webhook"


class DemoCase(str, Enum):
    BODY_COMPLETE = "body-complete"
    BODY_INCOMPLETE = "body-incomplete"
    WEBHOOK_BODY = "webhook-body"
    WEBHOOK_DOCUMENT_TXT = "webhook-document-txt"
    WEBHOOK_DOCUMENT_PDF = "webhook-document-pdf"
    WEBHOOK_DOCUMENT_MALFORMED = "webhook-document-malformed"
    WEBHOOK_DUPLICATE = "webhook-duplicate"
    WEBHOOK_CONFLICT = "webhook-conflict"


@dataclass(frozen=True)
class DemoCaseSpec:
    transport: SmokeTransport
    payload: dict[str, str]
    attachment: Path | None = None


class SmokeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    transport: Literal["intake", "webhook"]
    channel: str | None = None
    provider_id: str | None = None
    from_addr: str | None = None
    subject: str
    body: str
    attachment_path: str | None = None

    @model_validator(mode="after")
    def validate_transport_fields(self) -> "SmokeInput":
        if self.transport == "intake":
            if not self.channel or self.provider_id or self.from_addr or self.attachment_path:
                raise ValueError("invalid intake input")
        elif not self.provider_id or not self.from_addr or self.channel:
            raise ValueError("invalid webhook input")
        for name, value in (
            ("subject", self.subject),
            ("body", self.body),
            ("channel", self.channel),
            ("provider_id", self.provider_id),
            ("from_addr", self.from_addr),
            ("attachment_path", self.attachment_path),
        ):
            if value is not None and (not value.strip() or len(value) > 100_000):
                raise ValueError(f"invalid {name}")
        return self


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FREIGHT_DOCUMENT_FIXTURES = {
    "webhook-document-txt": PROJECT_ROOT / "evals" / "fixtures" / "docs" / "01-complete-en.txt",
    "webhook-document-pdf": PROJECT_ROOT / "evals" / "fixtures" / "docs" / "03-complete.pdf",
    "webhook-document-malformed": PROJECT_ROOT / "evals" / "fixtures" / "docs" / "08-malformed.pdf",
}

BODY_INCOMPLETE_PAYLOAD = {
    "channel": "email",
    "subject": "Synthetic incomplete freight request",
    "body": (
        "Origin: Chicago; destination: Detroit; equipment: dry_van; "
        "pickup_window: 2026-10-01T09:00:00Z; commodity: appliances"
    ),
}
DEMO_CASE_SPECS: dict[DemoCase, DemoCaseSpec] = {
    DemoCase.BODY_COMPLETE: DemoCaseSpec(
        transport=SmokeTransport.INTAKE,
        payload=dict(PROFILE_SCENARIOS["freight-broker"][1]),
    ),
    DemoCase.BODY_INCOMPLETE: DemoCaseSpec(
        transport=SmokeTransport.INTAKE,
        payload=BODY_INCOMPLETE_PAYLOAD,
    ),
    DemoCase.WEBHOOK_BODY: DemoCaseSpec(
        transport=SmokeTransport.WEBHOOK,
        payload={
            "subject": "Synthetic freight webhook",
            "body": "Synthetic freight webhook body",
        },
    ),
    DemoCase.WEBHOOK_DOCUMENT_TXT: DemoCaseSpec(
        transport=SmokeTransport.WEBHOOK,
        payload={
            "subject": "Synthetic text rate confirmation",
            "body": "Attached synthetic text document",
        },
        attachment=FREIGHT_DOCUMENT_FIXTURES[DemoCase.WEBHOOK_DOCUMENT_TXT.value],
    ),
    DemoCase.WEBHOOK_DOCUMENT_PDF: DemoCaseSpec(
        transport=SmokeTransport.WEBHOOK,
        payload={
            "subject": "Synthetic PDF rate confirmation",
            "body": "Attached synthetic PDF document",
        },
        attachment=FREIGHT_DOCUMENT_FIXTURES[DemoCase.WEBHOOK_DOCUMENT_PDF.value],
    ),
    DemoCase.WEBHOOK_DOCUMENT_MALFORMED: DemoCaseSpec(
        transport=SmokeTransport.WEBHOOK,
        payload={
            "subject": "Synthetic malformed rate confirmation",
            "body": "Attached malformed synthetic PDF",
        },
        attachment=FREIGHT_DOCUMENT_FIXTURES[
            DemoCase.WEBHOOK_DOCUMENT_MALFORMED.value
        ],
    ),
    DemoCase.WEBHOOK_DUPLICATE: DemoCaseSpec(
        transport=SmokeTransport.WEBHOOK,
        payload={
            "subject": "Synthetic duplicate webhook",
            "body": "Synthetic duplicate webhook body",
        },
    ),
    DemoCase.WEBHOOK_CONFLICT: DemoCaseSpec(
        transport=SmokeTransport.WEBHOOK,
        payload={
            "subject": "Synthetic conflicting webhook",
            "body": "Synthetic original webhook body",
        },
    ),
}


class SmokeFailure(RuntimeError):
    """Stable, source-free failure for the local smoke command."""

    def __init__(self, code: str, *, last_state: Mapping[str, object] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.last_state = dict(last_state) if last_state is not None else None


def _safe_execution_path(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > MAX_EXECUTION_STEPS:
        raise SmokeFailure("malformed_job_response")
    expected_keys = {
        "step",
        "action_key",
        "action_known",
        "policy_decision",
        "routing_status",
        "policy_reason",
        "missing_required_fields",
        "tool_outcome",
        "side_effect_committed",
    }
    decisions = {"allow", "deny", "needs_approval"}
    statuses = {"urgent", "awaiting_input", "pending_approval", "ready", "rejected"}
    reasons = {
        "safety_or_legal_risk",
        "unknown_intake_type",
        "missing_required_fields",
        "action_not_configured",
        "action_not_allowed",
        "approval_required",
        "action_allowed",
        "document_unreadable",
    }
    outcomes = {
        "success",
        "failed",
        "not_found",
        "invalid_arguments",
        "backend_failure",
        "no_result",
        "executed",
    }
    previous_step = 0
    safe: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise SmokeFailure("unsafe_job_response")
        step = item["step"]
        action_key = item["action_key"]
        action_known = item["action_known"]
        missing = item["missing_required_fields"]
        if (
            not isinstance(step, int)
            or isinstance(step, bool)
            or not 1 <= step <= MAX_EXECUTION_STEPS
            or step <= previous_step
            or not isinstance(action_known, bool)
            or (action_known and not isinstance(action_key, str))
            or (
                action_known
                and (
                    SAFE_IDENTIFIER_RE.fullmatch(action_key) is None
                    or len(action_key) > 64
                )
            )
            or (not action_known and action_key is not None)
            or not isinstance(item["policy_decision"], str)
            or item["policy_decision"] not in decisions
            or not isinstance(item["routing_status"], str)
            or item["routing_status"] not in statuses
            or not isinstance(item["policy_reason"], str)
            or item["policy_reason"] not in reasons
            or (
                item["tool_outcome"] is not None
                and (
                    not isinstance(item["tool_outcome"], str)
                    or item["tool_outcome"] not in outcomes
                )
            )
            or not isinstance(missing, list)
            or not all(
                isinstance(field, str)
                and SAFE_IDENTIFIER_RE.fullmatch(field) is not None
                for field in missing
            )
            or not isinstance(item["side_effect_committed"], bool)
        ):
            raise SmokeFailure("malformed_job_response")
        safe.append(dict(item))
        previous_step = step
    return safe


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
    agent_steps = value.get("agent_steps")
    if agent_steps is not None and (
        not isinstance(agent_steps, int)
        or isinstance(agent_steps, bool)
        or not 0 <= agent_steps <= MAX_EXECUTION_STEPS
    ):
        raise SmokeFailure("malformed_job_response")
    stop_reason = value.get("stop_reason")
    if stop_reason is not None and stop_reason not in {
        "final",
        "invalid_proposal",
        "repeated_tool",
        "max_steps",
        "executor_stopped",
        "tool_execution_failed",
    }:
        raise SmokeFailure("malformed_job_response")
    if "execution_path" in value:
        value["execution_path"] = _safe_execution_path(value["execution_path"])
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


def _read_attachment(path: Path) -> tuple[str, str]:
    media_type = WEBHOOK_DOCUMENT_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise SmokeFailure("attachment_type_unsupported")
    try:
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise SmokeFailure("attachment_too_large")
        content = path.read_bytes()
    except SmokeFailure:
        raise
    except OSError as error:
        raise SmokeFailure("attachment_read_error") from error
    return media_type, base64.b64encode(content).decode("ascii")


def _webhook_request(
    *,
    api_key: str,
    provider_id: str,
    from_addr: str,
    subject: str,
    body: str,
    attachment: Path | None,
    timestamp: int | None,
) -> tuple[bytes, dict[str, str]]:
    attachments: list[dict[str, str]] = []
    if attachment is not None:
        media_type, content_base64 = _read_attachment(attachment)
        attachments.append(
            {
                "media_type": media_type,
                "content_base64": content_base64,
            }
        )
    payload = {
        "provider_id": provider_id,
        "from_addr": from_addr,
        "subject": subject,
        "body": body,
        "attachments": attachments,
    }
    raw_body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    signed_at = int(time.time()) if timestamp is None else timestamp
    return raw_body, {
        "Content-Type": "application/json",
        "X-API-Key": api_key,
        "X-Inbound-Timestamp": str(signed_at),
        "X-Inbound-Signature": sign_inbound_webhook(
            api_key,
            signed_at,
            raw_body,
        ),
    }


def _accepted_identity(response: httpx.Response, *, error_prefix: str) -> tuple[str, str]:
    if response.status_code != 202:
        if response.status_code == 401:
            raise SmokeFailure(f"{error_prefix}_unauthorized")
        raise SmokeFailure(f"{error_prefix}_http_{response.status_code}")
    payload = _json_response(response)
    if not isinstance(payload, dict):
        raise SmokeFailure(f"malformed_{error_prefix}_response")
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or payload.get("status") != "queued":
        raise SmokeFailure(f"malformed_{error_prefix}_response")
    trace_id = response.headers.get("X-Trace-ID")
    if not trace_id:
        raise SmokeFailure("trace_id_missing")
    return job_id, trace_id


def _poll_job(
    *,
    http_client: httpx.Client,
    base_url: str,
    api_key: str,
    job_id: str,
    trace_id: str,
    expected_scenario: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> dict[str, object]:
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
            raise SmokeFailure("trace_id_mismatch", last_state=job)
        if job["scenario_key"] != expected_scenario:
            raise SmokeFailure("scenario_mismatch", last_state=job)
        if job["status"] in TERMINAL_STATUSES:
            if job["status"] in {"succeeded", "awaiting_approval"}:
                return job
            raise SmokeFailure(f"job_{job['status']}", last_state=job)
        poll_count += 1
        sleep(poll_interval_seconds)
    raise SmokeFailure("poll_timeout", last_state=last_state)


def run_smoke(
    *,
    profile: str,
    base_url: str,
    api_key: str,
    payload: Mapping[str, str] | None = None,
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

    expected_scenario, default_payload = PROFILE_SCENARIOS[profile]
    request_payload = dict(default_payload if payload is None else payload)
    if set(request_payload) != {"channel", "subject", "body"} or not all(
        isinstance(value, str) for value in request_payload.values()
    ):
        raise SmokeFailure("input_payload_invalid")
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
                json=request_payload,
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


def run_webhook_smoke(
    *,
    profile: str,
    base_url: str,
    api_key: str,
    subject: str = "Synthetic freight webhook",
    body: str = "Synthetic freight webhook body",
    provider_id: str | None = None,
    from_addr: str = "dispatcher@example.test",
    attachment: Path | None = None,
    timestamp: int | None = None,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 1.0,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Send one real signed webhook and return its safe terminal job state."""

    if not api_key.strip():
        raise SmokeFailure("tenant_api_key_missing")
    if profile not in PROFILE_SCENARIOS:
        raise SmokeFailure("profile_invalid")
    if profile != "freight-broker":
        raise SmokeFailure("webhook_profile_unsupported")
    if (
        not math.isfinite(timeout_seconds)
        or not math.isfinite(poll_interval_seconds)
        or timeout_seconds <= 0
        or poll_interval_seconds < 0
    ):
        raise SmokeFailure("poll_bounds_invalid")

    expected_scenario = PROFILE_SCENARIOS[profile][0]
    provider_id = provider_id or f"live-{uuid4()}"
    owned_client = client is None
    http_client = client or httpx.Client(timeout=10.0)
    try:
        raw_body, headers = _webhook_request(
            api_key=api_key,
            provider_id=provider_id,
            from_addr=from_addr,
            subject=subject,
            body=body,
            attachment=attachment,
            timestamp=timestamp,
        )
        try:
            accepted = http_client.post(
                f"{base_url.rstrip('/')}/v1/inbound/email/webhook",
                headers=headers,
                content=raw_body,
            )
        except httpx.RequestError as error:
            raise SmokeFailure("webhook_network_error") from error
        job_id, trace_id = _accepted_identity(accepted, error_prefix="webhook")
        return _poll_job(
            http_client=http_client,
            base_url=base_url,
            api_key=api_key,
            job_id=job_id,
            trace_id=trace_id,
            expected_scenario=expected_scenario,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            clock=clock,
            sleep=sleep,
        )
    finally:
        if owned_client:
            http_client.close()


def run_webhook_idempotency(
    *,
    profile: str,
    base_url: str,
    api_key: str,
    conflict: bool,
    subject: str = "Synthetic freight webhook",
    body: str = "Synthetic freight webhook body",
    provider_id: str | None = None,
    from_addr: str = "dispatcher@example.test",
    attachment: Path | None = None,
    timestamp: int | None = None,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 1.0,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, object], bool, bool]:
    """Exercise duplicate reuse or changed-payload conflict over real HTTP."""

    if not api_key.strip():
        raise SmokeFailure("tenant_api_key_missing")
    if profile != "freight-broker":
        raise SmokeFailure("webhook_profile_unsupported")
    if (
        not math.isfinite(timeout_seconds)
        or not math.isfinite(poll_interval_seconds)
        or timeout_seconds <= 0
        or poll_interval_seconds < 0
    ):
        raise SmokeFailure("poll_bounds_invalid")

    provider_id = provider_id or f"live-{uuid4()}"
    expected_scenario = PROFILE_SCENARIOS[profile][0]
    owned_client = client is None
    http_client = client or httpx.Client(timeout=10.0)
    try:
        first_raw, first_headers = _webhook_request(
            api_key=api_key,
            provider_id=provider_id,
            from_addr=from_addr,
            subject=subject,
            body=body,
            attachment=attachment,
            timestamp=timestamp,
        )
        try:
            first = http_client.post(
                f"{base_url.rstrip('/')}/v1/inbound/email/webhook",
                headers=first_headers,
                content=first_raw,
            )
        except httpx.RequestError as error:
            raise SmokeFailure("webhook_network_error") from error
        first_job_id, first_trace_id = _accepted_identity(
            first,
            error_prefix="webhook",
        )
        second_body = "Changed synthetic freight webhook body" if conflict else body
        second_raw, second_headers = _webhook_request(
            api_key=api_key,
            provider_id=provider_id,
            from_addr=from_addr,
            subject=subject,
            body=second_body,
            attachment=attachment,
            timestamp=(None if timestamp is None else timestamp + 1),
        )
        try:
            second = http_client.post(
                f"{base_url.rstrip('/')}/v1/inbound/email/webhook",
                headers=second_headers,
                content=second_raw,
            )
        except httpx.RequestError as error:
            raise SmokeFailure("webhook_network_error") from error
        if conflict:
            if second.status_code != 409:
                raise SmokeFailure("expected_conflict_not_returned")
            conflict_rejected = True
            duplicate_reused = False
        else:
            second_job_id, second_trace_id = _accepted_identity(
                second,
                error_prefix="webhook",
            )
            if (second_job_id, second_trace_id) != (first_job_id, first_trace_id):
                raise SmokeFailure("duplicate_identity_mismatch")
            duplicate_reused = True
            conflict_rejected = False
        job = _poll_job(
            http_client=http_client,
            base_url=base_url,
            api_key=api_key,
            job_id=first_job_id,
            trace_id=first_trace_id,
            expected_scenario=expected_scenario,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            clock=clock,
            sleep=sleep,
        )
        return job, duplicate_reused, conflict_rejected
    finally:
        if owned_client:
            http_client.close()


def _load_custom_input(path: Path) -> tuple[SmokeInput, Path | None]:
    try:
        if path.stat().st_size > 1_000_000:
            raise SmokeFailure("input_file_too_large")
        raw = path.read_text(encoding="utf-8")
    except SmokeFailure:
        raise
    except (OSError, UnicodeError) as error:
        raise SmokeFailure("input_file_invalid") from error
    try:
        value = SmokeInput.model_validate_json(raw)
    except (ValidationError, ValueError) as error:
        raise SmokeFailure("input_file_invalid") from error
    if value.attachment_path is None:
        return value, None
    attachment_root = path.resolve().parent
    attachment_input = Path(value.attachment_path)
    if attachment_input.is_absolute() or attachment_input.drive:
        raise SmokeFailure("attachment_path_invalid")
    attachment = (attachment_root / attachment_input).resolve()
    try:
        attachment.relative_to(attachment_root)
    except ValueError as error:
        raise SmokeFailure("attachment_path_invalid") from error
    return value, attachment


def _check(name: str, expected: object, actual: object) -> dict[str, object]:
    return {
        "name": name,
        "expected": expected,
        "actual": actual,
        "passed": expected == actual,
    }


def _report(
    *,
    demo_case: str | None,
    profile: str,
    transport: SmokeTransport,
    endpoint: str,
    input_kind: str,
    job: Mapping[str, object],
    checks: list[dict[str, object]],
    duplicate_reused: bool | None = None,
    conflict_rejected: bool | None = None,
) -> dict[str, object]:
    return {
        "result": "PASS" if all(check["passed"] for check in checks) else "FAIL",
        "demo_case": demo_case,
        "profile": profile,
        "transport": transport.value,
        "endpoint": endpoint,
        "input_kind": input_kind,
        "acceptance_status": 202,
        "duplicate_reused": duplicate_reused,
        "conflict_rejected": conflict_rejected,
        "checks": checks,
        "job": dict(job),
        "error_code": None,
    }


def _check_execution_path(
    job: Mapping[str, object],
    expected_path: str | None,
) -> dict[str, object] | None:
    if expected_path is None:
        return None
    path = job.get("execution_path", [])
    observed = False
    if isinstance(path, list):
        if expected_path == "tool":
            observed = any(item.get("tool_outcome") is not None for item in path)
        elif expected_path == "approval":
            observed = any(
                item.get("policy_decision") == "needs_approval" for item in path
            )
    return _check(f"execution_path_{expected_path}", True, observed)


def _case_checks(
    case: DemoCase,
    job: Mapping[str, object],
    *,
    duplicate_reused: bool | None = None,
    conflict_rejected: bool | None = None,
) -> list[dict[str, object]]:
    checks = [
        _check("terminal_status", True, job.get("status") in {"succeeded", "awaiting_approval"}),
    ]
    if case is DemoCase.BODY_COMPLETE:
        approval = job.get("approval")
        checks.append(
            _check(
                "approval_evidence_when_pending",
                True,
                job.get("status") != "awaiting_approval"
                or (isinstance(approval, dict) and isinstance(approval.get("approval_id"), str)),
            )
        )
        checks.append(
            _check(
                "approval_execution_path_when_pending",
                True,
                job.get("status") != "awaiting_approval"
                or (
                    isinstance(job.get("execution_path"), list)
                    and any(
                        isinstance(item, dict)
                        and item.get("policy_decision") == "needs_approval"
                        for item in job["execution_path"]
                    )
                ),
            )
        )
    elif case is DemoCase.BODY_INCOMPLETE:
        checks.extend(
            [
                _check("status", "succeeded", job.get("status")),
                _check("routing_status", "awaiting_input", job.get("routing_status")),
                _check("missing_required_fields", ["contact"], job.get("missing_required_fields")),
                _check(
                    "no_side_effect",
                    True,
                    not any(
                        item.get("side_effect_committed")
                        for item in job.get("execution_path", [])
                        if isinstance(item, dict)
                    ),
                ),
            ]
        )
    elif case is DemoCase.WEBHOOK_DOCUMENT_MALFORMED:
        checks.extend(
            [
                _check("status", "succeeded", job.get("status")),
                _check("routing_status", "awaiting_input", job.get("routing_status")),
                _check("routing_reason", "document_unreadable", job.get("routing_reason")),
                _check("agent_steps", 0, job.get("agent_steps")),
                _check("execution_path", [], job.get("execution_path")),
                _check("approval", None, job.get("approval")),
            ]
        )
    elif case is DemoCase.WEBHOOK_DUPLICATE:
        checks.append(_check("duplicate_reused", True, duplicate_reused))
    elif case is DemoCase.WEBHOOK_CONFLICT:
        checks.append(_check("conflict_rejected", True, conflict_rejected))
    return checks


def run_demo_case(
    *,
    case: DemoCase | str,
    profile: str,
    base_url: str,
    api_key: str,
    expect_path: str | None = None,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 1.0,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    try:
        selected = case if isinstance(case, DemoCase) else DemoCase(case)
    except ValueError as error:
        raise SmokeFailure("demo_case_invalid") from error
    spec = DEMO_CASE_SPECS[selected]
    if spec.transport is SmokeTransport.WEBHOOK and profile != "freight-broker":
        raise SmokeFailure("webhook_profile_unsupported")
    duplicate_reused: bool | None = None
    conflict_rejected: bool | None = None
    if spec.transport is SmokeTransport.INTAKE:
        job = run_smoke(
            profile=profile,
            base_url=base_url,
            api_key=api_key,
            payload=spec.payload,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            client=client,
            clock=clock,
            sleep=sleep,
        )
        input_kind = "body"
    elif selected is DemoCase.WEBHOOK_DUPLICATE:
        job, duplicate_reused, conflict_rejected = run_webhook_idempotency(
            profile=profile,
            base_url=base_url,
            api_key=api_key,
            conflict=False,
            subject=spec.payload["subject"],
            body=spec.payload["body"],
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            client=client,
            clock=clock,
            sleep=sleep,
        )
        input_kind = "webhook body"
    elif selected is DemoCase.WEBHOOK_CONFLICT:
        job, duplicate_reused, conflict_rejected = run_webhook_idempotency(
            profile=profile,
            base_url=base_url,
            api_key=api_key,
            conflict=True,
            subject=spec.payload["subject"],
            body=spec.payload["body"],
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            client=client,
            clock=clock,
            sleep=sleep,
        )
        input_kind = "webhook conflict"
    else:
        job = run_webhook_smoke(
            profile=profile,
            base_url=base_url,
            api_key=api_key,
            subject=spec.payload["subject"],
            body=spec.payload["body"],
            attachment=spec.attachment,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            client=client,
            clock=clock,
            sleep=sleep,
        )
        input_kind = (
            "text attachment"
            if selected is DemoCase.WEBHOOK_DOCUMENT_TXT
            else "PDF attachment"
            if selected in {
                DemoCase.WEBHOOK_DOCUMENT_PDF,
                DemoCase.WEBHOOK_DOCUMENT_MALFORMED,
            }
            else "webhook body"
        )
    checks = _case_checks(
        selected,
        job,
        duplicate_reused=duplicate_reused,
        conflict_rejected=conflict_rejected,
    )
    path_check = _check_execution_path(job, expect_path)
    if path_check is not None:
        checks.append(path_check)
    if path_check is not None and not path_check["passed"]:
        raise SmokeFailure("expected_path_not_observed", last_state=job)
    if not all(check["passed"] for check in checks):
        raise SmokeFailure("expectation_failed", last_state=job)
    return _report(
        demo_case=selected.value,
        profile=profile,
        transport=spec.transport,
        endpoint=(
            "/v1/intake"
            if spec.transport is SmokeTransport.INTAKE
            else "/v1/inbound/email/webhook"
        ),
        input_kind=input_kind,
        job=job,
        checks=checks,
        duplicate_reused=duplicate_reused,
        conflict_rejected=conflict_rejected,
    )


def run_custom_input(
    *,
    input_file: Path,
    profile: str,
    base_url: str,
    api_key: str,
    expect_path: str | None = None,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 1.0,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    value, attachment = _load_custom_input(input_file)
    selected_transport = SmokeTransport(value.transport)
    if selected_transport is SmokeTransport.INTAKE:
        payload = {
            "channel": value.channel,
            "subject": value.subject,
            "body": value.body,
        }
        job = run_smoke(
            profile=profile,
            base_url=base_url,
            api_key=api_key,
            payload=payload,  # type: ignore[arg-type]
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            client=client,
            clock=clock,
            sleep=sleep,
        )
        input_kind = "body"
        endpoint = "/v1/intake"
    else:
        if profile != "freight-broker":
            raise SmokeFailure("webhook_profile_unsupported")
        job = run_webhook_smoke(
            profile=profile,
            base_url=base_url,
            api_key=api_key,
            provider_id=value.provider_id,
            from_addr=value.from_addr or "",
            subject=value.subject,
            body=value.body,
            attachment=attachment,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            client=client,
            clock=clock,
            sleep=sleep,
        )
        input_kind = (
            "text attachment"
            if attachment is not None and attachment.suffix.lower() == ".txt"
            else "PDF attachment"
            if attachment is not None
            else "webhook body"
        )
        endpoint = "/v1/inbound/email/webhook"
    checks = [_check("terminal_status", True, job["status"] in {"succeeded", "awaiting_approval"})]
    path_check = _check_execution_path(job, expect_path)
    if path_check is not None:
        checks.append(path_check)
    if path_check is not None and not path_check["passed"]:
        raise SmokeFailure("expected_path_not_observed", last_state=job)
    if not all(check["passed"] for check in checks):
        raise SmokeFailure("expectation_failed", last_state=job)
    return _report(
        demo_case=None,
        profile=profile,
        transport=selected_transport,
        endpoint=endpoint,
        input_kind=input_kind,
        job=job,
        checks=checks,
    )


def _duration_ms(start: object, finish: object) -> int | None:
    if not isinstance(start, str) or not isinstance(finish, str):
        return None
    try:
        started = datetime.fromisoformat(start.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(finish.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((ended - started).total_seconds() * 1000))


def render_human(report: Mapping[str, object]) -> str:
    job = report.get("job")
    safe_job = job if isinstance(job, Mapping) else {}
    path = safe_job.get("execution_path")
    lines = [
        f"LIVE INTAKE SMOKE: {_display(report.get('result', 'FAIL'))}",
        f"Case:             {_display(report.get('demo_case') or 'custom')}",
        f"Profile:          {_display(report.get('profile'))}",
        f"Transport:        {_display(report.get('transport'))}",
        f"Endpoint:         {_display(report.get('endpoint'))}",
        f"Input:            {_display(report.get('input_kind'))}",
        f"HTTP accepted:    {_display(report.get('acceptance_status'))}",
        f"Job ID:           {_display(safe_job.get('job_id'))}",
        f"Trace ID:         {_display(safe_job.get('trace_id'))}",
        f"Scenario:         {_display(safe_job.get('scenario_key'))}",
        f"Status:           {_display(safe_job.get('status'))}",
        f"Worker attempts:  {_display(safe_job.get('attempt_count'))}",
        f"Agent steps:      {_display(safe_job.get('agent_steps'))}",
        f"Stop reason:      {_display(safe_job.get('stop_reason'))}",
        f"Routing:          {_routing_text(safe_job)}",
        f"Missing fields:   {_list_text(safe_job.get('missing_required_fields'))}",
        f"Approval:         {_approval_text(safe_job.get('approval'))}",
        f"Created at:       {_display(safe_job.get('created_at'))}",
        f"Started at:       {_display(safe_job.get('started_at'))}",
        f"Finished at:      {_display(safe_job.get('finished_at'))}",
        f"Error:            {_display(safe_job.get('error_code'))}",
        f"Smoke error:      {_display(report.get('error_code'))}",
        f"Durations (ms):   queue={_duration_text(safe_job.get('created_at'), safe_job.get('started_at'))} "
        f"run={_duration_text(safe_job.get('started_at'), safe_job.get('finished_at'))} "
        f"total={_duration_text(safe_job.get('created_at'), safe_job.get('finished_at'))}",
        "Execution path:",
    ]
    if isinstance(path, list) and path:
        for item in path:
            if isinstance(item, Mapping):
                lines.append(
                    "  "
                    f"{_display(item.get('step'))}. "
                    f"action={_display(item.get('action_key'))} "
                    f"| known={_display(item.get('action_known'))} "
                    f"| policy={_display(item.get('policy_decision'))} "
                    f"| routing={_display(item.get('routing_status'))} "
                    f"| reason={_display(item.get('policy_reason'))} "
                    f"| missing={_list_text(item.get('missing_required_fields'))} "
                    f"| tool={_display(item.get('tool_outcome'))} "
                    f"| side_effect={_side_effect_text(item.get('side_effect_committed'))}"
                )
    else:
        lines.append("  NONE")
    lines.extend(
        [
            f"Duplicate reused: {_display(report.get('duplicate_reused'))}",
            f"Conflict rejected: {_display(report.get('conflict_rejected'))}",
            "Checks:",
        ]
    )
    checks = report.get("checks")
    if isinstance(checks, list) and checks:
        for check in checks:
            if isinstance(check, Mapping):
                lines.append(
                    f"  {'PASS' if check.get('passed') else 'FAIL'} "
                    f"{check.get('name', 'unknown')} "
                    f"expected={_display(check.get('expected'))} "
                    f"actual={_display(check.get('actual'))}"
                )
    else:
        lines.append("  NONE")
    return "\n".join(lines)


def _list_text(value: object) -> str:
    if value is None:
        return "NOT OBSERVED"
    if not isinstance(value, list) or not value:
        return "NONE"
    return ", ".join(str(item) for item in value)


def _display(value: object) -> object:
    return "NOT OBSERVED" if value is None else value


def _side_effect_text(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "NOT OBSERVED"


def _duration_text(start: object, finish: object) -> int | str:
    value = _duration_ms(start, finish)
    return "NOT OBSERVED" if value is None else value


def _routing_text(job: Mapping[str, object]) -> str:
    status = job.get("routing_status")
    reason = job.get("routing_reason")
    if status is None and reason is None:
        return "NOT OBSERVED"
    return f"{status or 'NOT OBSERVED'} / {reason or 'NOT OBSERVED'}"


def _approval_text(value: object) -> str:
    if not isinstance(value, Mapping):
        return "NOT OBSERVED"
    return f"{_display(value.get('approval_id'))} / {_display(value.get('status'))}"


def _report_for_single_job(
    *,
    profile: str,
    transport: SmokeTransport,
    endpoint: str,
    input_kind: str,
    job: Mapping[str, object],
    expect_path: str | None,
) -> dict[str, object]:
    checks = [_check("terminal_status", True, job.get("status") in {"succeeded", "awaiting_approval"})]
    path_check = _check_execution_path(job, expect_path)
    if path_check is not None:
        checks.append(path_check)
    if path_check is not None and not path_check["passed"]:
        raise SmokeFailure("expected_path_not_observed", last_state=job)
    if not all(check["passed"] for check in checks):
        raise SmokeFailure("expectation_failed", last_state=job)
    return _report(
        demo_case=None,
        profile=profile,
        transport=transport,
        endpoint=endpoint,
        input_kind=input_kind,
        job=job,
        checks=checks,
    )


def run_live_report(
    *,
    profile: str,
    base_url: str,
    api_key: str,
    transport: SmokeTransport | None = None,
    demo_case: str | None = None,
    input_file: Path | None = None,
    attachment: Path | None = None,
    provider_id: str | None = None,
    from_addr: str = "dispatcher@example.test",
    subject: str | None = None,
    body: str | None = None,
    expect_path: str | None = None,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 1.0,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    if demo_case is not None and input_file is not None:
        raise SmokeFailure("demo_case_and_input_file_conflict")
    if demo_case is not None:
        try:
            selected_case = DemoCase(demo_case)
        except ValueError as error:
            raise SmokeFailure("demo_case_invalid") from error
        if transport is not None and transport is not DEMO_CASE_SPECS[selected_case].transport:
            raise SmokeFailure("transport_case_mismatch")
        if attachment is not None:
            raise SmokeFailure("attachment_case_conflict")
        return run_demo_case(
            case=demo_case,
            profile=profile,
            base_url=base_url,
            api_key=api_key,
            expect_path=expect_path,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            client=client,
            clock=clock,
            sleep=sleep,
        )
    if input_file is not None:
        if attachment is not None:
            raise SmokeFailure("attachment_input_file_conflict")
        value, _ = _load_custom_input(input_file)
        if transport is not None and transport.value != value.transport:
            raise SmokeFailure("transport_input_file_mismatch")
        return run_custom_input(
            input_file=input_file,
            profile=profile,
            base_url=base_url,
            api_key=api_key,
            expect_path=expect_path,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            client=client,
            clock=clock,
            sleep=sleep,
        )
    selected_transport = transport or (
        SmokeTransport.WEBHOOK if attachment is not None else SmokeTransport.INTAKE
    )
    if selected_transport is SmokeTransport.INTAKE:
        if attachment is not None:
            raise SmokeFailure("attachment_transport_invalid")
        default_payload = dict(PROFILE_SCENARIOS[profile][1])
        if subject is not None:
            default_payload["subject"] = subject
        if body is not None:
            default_payload["body"] = body
        job = run_smoke(
            profile=profile,
            base_url=base_url,
            api_key=api_key,
            payload=default_payload,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            client=client,
            clock=clock,
            sleep=sleep,
        )
        return _report_for_single_job(
            profile=profile,
            transport=selected_transport,
            endpoint="/v1/intake",
            input_kind="body",
            job=job,
            expect_path=expect_path,
        )
    job = run_webhook_smoke(
        profile=profile,
        base_url=base_url,
        api_key=api_key,
        provider_id=provider_id,
        from_addr=from_addr,
        subject=subject or "Synthetic freight webhook",
        body=body or "Synthetic freight webhook body",
        attachment=attachment,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        client=client,
        clock=clock,
        sleep=sleep,
    )
    return _report_for_single_job(
        profile=profile,
        transport=selected_transport,
        endpoint="/v1/inbound/email/webhook",
        input_kind=(
            "text attachment"
            if attachment is not None and attachment.suffix.lower() == ".txt"
            else "PDF attachment"
            if attachment is not None
            else "webhook body"
        ),
        job=job,
        expect_path=expect_path,
    )


def _failure_report(arguments: argparse.Namespace, error: SmokeFailure) -> dict[str, object]:
    selected_transport = arguments.transport or (
        SmokeTransport.WEBHOOK.value if arguments.attachment else SmokeTransport.INTAKE.value
    )
    endpoint = (
        "/v1/inbound/email/webhook"
        if selected_transport == SmokeTransport.WEBHOOK.value
        else "/v1/intake"
    )
    input_kind = (
        "custom input"
        if arguments.input_file is not None
        else "attachment"
        if arguments.attachment is not None
        else "webhook body"
        if selected_transport == SmokeTransport.WEBHOOK.value
        else "body"
    )
    return {
        "result": "FAIL",
        "demo_case": arguments.demo_case,
        "profile": arguments.profile,
        "transport": selected_transport,
        "endpoint": endpoint,
        "input_kind": input_kind,
        "acceptance_status": None,
        "duplicate_reused": None,
        "conflict_rejected": None,
        "checks": [],
        "job": error.last_state or {},
        "error_code": error.code,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(PROFILE_SCENARIOS), default="freight-broker")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--transport", choices=tuple(item.value for item in SmokeTransport))
    parser.add_argument("--demo-case", choices=tuple(item.value for item in DemoCase))
    parser.add_argument("--input-file", type=Path)
    parser.add_argument("--attachment", type=Path)
    parser.add_argument("--provider-id")
    parser.add_argument("--from-addr", default="dispatcher@example.test")
    parser.add_argument("--subject")
    parser.add_argument("--body")
    parser.add_argument("--expect-path", choices=("tool", "approval"))
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)
    api_key = os.environ.get("INTEGRA_DEMO_API_KEY", "")
    transport = (
        SmokeTransport(arguments.transport) if arguments.transport is not None else None
    )
    try:
        report = run_live_report(
            profile=arguments.profile,
            base_url=arguments.base_url,
            api_key=api_key,
            transport=transport,
            demo_case=arguments.demo_case,
            input_file=arguments.input_file,
            attachment=arguments.attachment,
            provider_id=arguments.provider_id,
            from_addr=arguments.from_addr,
            subject=arguments.subject,
            body=arguments.body,
            expect_path=arguments.expect_path,
            timeout_seconds=arguments.timeout_seconds,
            poll_interval_seconds=arguments.poll_interval_seconds,
        )
    except SmokeFailure as error:
        output = _failure_report(arguments, error)
        if arguments.json:
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        else:
            print(render_human(output))
        return 2 if error.code in {
            "tenant_api_key_missing",
            "profile_invalid",
            "poll_bounds_invalid",
            "input_file_invalid",
            "input_file_too_large",
            "attachment_type_unsupported",
            "attachment_too_large",
            "attachment_read_error",
            "attachment_path_invalid",
            "demo_case_invalid",
            "demo_case_and_input_file_conflict",
            "transport_case_mismatch",
            "attachment_case_conflict",
            "attachment_input_file_conflict",
            "transport_input_file_mismatch",
            "attachment_transport_invalid",
            "input_payload_invalid",
        } else 1
    if arguments.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(render_human(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
