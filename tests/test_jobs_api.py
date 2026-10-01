from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_tenant
from app.db.models import Tenant
from app.db.session import get_db_session
from app.domain.job_repository import JobRepository
from app.main import app
from app.runtime.profiles import TenantProfileResolver


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
JOB_ID = uuid4()
TRACE_ID = uuid4()


@pytest.fixture
def freight_profile():
    return TenantProfileResolver(PROJECT_ROOT / "examples").resolve("freight-broker")


def _row(profile, *, status: str = "succeeded", approval_id=None, approval_status=None):
    from app.domain.jobs import PersistedJobRead

    return PersistedJobRead(
        job_id=JOB_ID,
        trace_id=TRACE_ID,
        status=status,
        attempt_count=2,
        result={
            "status": "completed",
            "reason": "final",
            "steps": 1,
            "final_response": "secret model rationale",
            "routing_status": "ready",
            "routing_reason": "complete",
            "missing_required_fields": ["contact", "contact"],
            "tool_result": {"body": "secret"},
        },
        error_code=None,
        tenant_config_snapshot=profile.snapshot,
        tenant_config_sha256=profile.sha256,
        approval_id=approval_id,
        approval_status=approval_status,
        created_at=NOW,
        started_at=NOW,
        finished_at=NOW,
    )


def test_project_job_read_is_an_allowlisted_source_free_projection(freight_profile) -> None:
    from app.domain.jobs import project_job_read

    safe = project_job_read(_row(freight_profile))

    assert safe.model_dump(mode="json") == {
        "job_id": str(JOB_ID),
        "trace_id": str(TRACE_ID),
        "scenario_key": "freight_broker",
        "status": "succeeded",
        "attempt_count": 2,
        "routing_status": "ready",
        "routing_reason": "complete",
        "missing_required_fields": ["contact"],
        "approval": None,
        "error_code": None,
        "created_at": NOW.isoformat().replace("+00:00", "Z"),
        "started_at": NOW.isoformat().replace("+00:00", "Z"),
        "finished_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    rendered = safe.model_dump_json()
    assert "secret" not in rendered
    assert "final_response" not in rendered
    assert "tool_result" not in rendered


@pytest.mark.parametrize(
    "status",
    ["queued", "running", "awaiting_approval", "succeeded", "failed", "failed_uncertain"],
)
def test_project_job_read_accepts_only_closed_job_states(freight_profile, status: str) -> None:
    from app.domain.jobs import project_job_read

    assert project_job_read(_row(freight_profile, status=status)).status == status


def test_project_job_read_includes_only_joined_approval_identity(freight_profile) -> None:
    from app.domain.jobs import project_job_read

    approval_id = uuid4()
    safe = project_job_read(
        _row(
            freight_profile,
            status="awaiting_approval",
            approval_id=approval_id,
            approval_status="pending",
        )
    )

    assert safe.approval is not None
    assert safe.approval.approval_id == approval_id
    assert safe.approval.status == "pending"


def test_project_job_read_rejects_unknown_status(freight_profile) -> None:
    from app.domain.jobs import project_job_read

    with pytest.raises(ValueError, match="unsupported job status"):
        project_job_read(_row(freight_profile, status="unknown"))


def test_project_job_read_verifies_profile_hash_before_projection(freight_profile) -> None:
    from app.domain.jobs import project_job_read

    row = _row(freight_profile)
    row.tenant_config_snapshot["display_name"] = "tampered"

    with pytest.raises(RuntimeError, match="profile"):
        project_job_read(row)


def test_job_api_returns_only_safe_own_tenant_projection(monkeypatch, freight_profile) -> None:
    from app.domain.jobs import PersistedJobRead

    tenant_id = uuid4()
    tenant = Tenant(id=tenant_id, slug="freight-broker", name="Freight")
    row = PersistedJobRead(
        job_id=JOB_ID,
        trace_id=TRACE_ID,
        status="succeeded",
        attempt_count=1,
        result={
            "routing_status": "ready",
            "routing_reason": "complete",
            "missing_required_fields": [],
            "final_response": "secret",
        },
        error_code="provider_unavailable",
        tenant_config_snapshot=freight_profile.snapshot,
        tenant_config_sha256=freight_profile.sha256,
        approval_id=None,
        approval_status=None,
        created_at=NOW,
        started_at=NOW,
        finished_at=NOW,
    )

    async def read_for_tenant(self, job_id, requested_tenant_id):
        if job_id == JOB_ID and requested_tenant_id == tenant_id:
            return row
        return None

    monkeypatch.setattr(JobRepository, "get_read_for_tenant", read_for_tenant)

    async def override_tenant():
        return tenant

    async def override_session():
        yield object()

    app.dependency_overrides[get_current_tenant] = override_tenant
    app.dependency_overrides[get_db_session] = override_session
    try:
        response = TestClient(app).get(f"/v1/jobs/{JOB_ID}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert set(response.json()) == {
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
    assert "secret" not in response.text
    assert "final_response" not in response.text
    assert "source_snapshot" not in response.text


@pytest.mark.parametrize("requested_job_id", [uuid4(), JOB_ID])
def test_job_api_hides_unknown_and_cross_tenant_jobs(monkeypatch, requested_job_id) -> None:
    tenant = Tenant(id=uuid4(), slug="freight-broker", name="Freight")

    async def read_for_tenant(self, job_id, requested_tenant_id):
        return None

    monkeypatch.setattr(JobRepository, "get_read_for_tenant", read_for_tenant)

    async def override_tenant():
        return tenant

    async def override_session():
        yield object()

    app.dependency_overrides[get_current_tenant] = override_tenant
    app.dependency_overrides[get_db_session] = override_session
    try:
        response = TestClient(app).get(f"/v1/jobs/{requested_job_id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found"}
