from __future__ import annotations

from dataclasses import dataclass
import time
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.auth import (
    get_current_inbound_tenant,
    get_current_tenant,
    sign_inbound_webhook,
)
from app.auth.inbound_webhook import (
    InboundSignatureOutcome,
    inbound_signature_outcome,
)
from app.db.models import Tenant
from app.db.session import get_db_session
from app.observability import (
    Component,
    EventName,
    ObservationContext,
    OutcomeCode,
    RecordingObserver,
    RouteName,
)
from app.observability.middleware import (
    ObservabilityMiddleware,
    bind_request_context,
    request_context,
    request_observer,
)


@dataclass
class _ScalarResult:
    value: Tenant | None

    def scalar_one_or_none(self) -> Tenant | None:
        return self.value


class _Session:
    def __init__(self, tenant: Tenant | None) -> None:
        self.tenant = tenant

    async def execute(self, statement: object) -> _ScalarResult:
        del statement
        return _ScalarResult(self.tenant)


def _build_observed_app(observer: RecordingObserver) -> FastAPI:
    observed = FastAPI()
    observed.state.observer = observer
    observed.add_middleware(ObservabilityMiddleware, observer=observer)

    @observed.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @observed.get("/v1/cases/{case_id}")
    async def case(case_id: UUID) -> dict[str, str]:
        return {"case_id": str(case_id)}

    @observed.get("/boom")
    async def boom() -> None:
        raise RuntimeError("SOURCE_BODY_SECRET")

    return observed


def test_every_request_gets_a_server_trace_header_and_completed_event() -> None:
    observer = RecordingObserver()
    client = TestClient(_build_observed_app(observer))

    response = client.get("/health")

    assert response.status_code == 200
    trace_id = UUID(response.headers["X-Trace-ID"])
    events = [event for event in observer.events if event.event is EventName.HTTP_REQUEST_COMPLETED]
    assert len(events) == 1
    assert events[0].trace_id == trace_id
    assert events[0].route is RouteName.HEALTH
    assert events[0].component is Component.API
    assert events[0].outcome is OutcomeCode.SUCCESS


def test_valid_client_correlation_is_request_only_and_client_trace_is_ignored() -> None:
    observer = RecordingObserver()
    client = TestClient(_build_observed_app(observer))

    response = client.get(
        "/v1/cases/00000000-0000-0000-0000-000000000001?secret=SOURCE_BODY_SECRET",
        headers={
            "X-Correlation-ID": "support.case-42",
            "X-Trace-ID": "33333333-3333-3333-3333-333333333333",
        },
    )

    event = observer.events[-1]
    assert response.status_code == 200
    assert response.headers["X-Trace-ID"] != "33333333-3333-3333-3333-333333333333"
    assert event.client_correlation_id == "support.case-42"
    assert event.route is RouteName.CASE
    serialized = event.model_dump_json()
    assert "SOURCE_BODY_SECRET" not in serialized
    assert "?secret" not in serialized


def test_invalid_or_oversized_client_correlation_is_not_echoed_or_logged() -> None:
    observer = RecordingObserver()
    client = TestClient(_build_observed_app(observer))

    response = client.get("/health", headers={"X-Correlation-ID": "!" * 129})

    event = observer.events[-1]
    assert response.status_code == 200
    assert "X-Correlation-ID" not in response.headers
    assert event.client_correlation_id is None
    assert event.client_correlation_invalid is True
    assert "!" not in event.model_dump_json()


def test_route_template_and_safe_500_event_do_not_expose_raw_path_or_exception() -> None:
    observer = RecordingObserver()
    client = TestClient(_build_observed_app(observer), raise_server_exceptions=False)

    response = client.get("/boom?body=SOURCE_BODY_SECRET")

    event = observer.events[-1]
    assert response.status_code == 500
    assert event.status_code == 500
    assert event.route is RouteName.UNKNOWN
    rendered = event.model_dump_json()
    assert "/boom" not in rendered
    assert "SOURCE_BODY_SECRET" not in rendered


def test_request_state_helpers_bind_authoritative_trace() -> None:
    observer = RecordingObserver()
    app = _build_observed_app(observer)
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    event = observer.events[-1]
    assert event.request_id is not None
    context = ObservationContext(trace_id=event.trace_id, request_id=event.request_id)
    assert context.bind(job_id=uuid4()).trace_id == event.trace_id


def test_request_completion_carries_bound_scenario_and_profile_identity() -> None:
    observer = RecordingObserver()
    app = _build_observed_app(observer)

    @app.get("/profile-bound")
    async def profile_bound(request: Request) -> dict[str, str]:
        bind_request_context(
            request,
            request_context(request).bind(
                scenario_key="repair_service",
                profile_fingerprint="b" * 64,
            ),
        )
        return {"status": "ok"}

    response = TestClient(app).get("/profile-bound")

    event = [
        item
        for item in observer.events
        if item.event is EventName.HTTP_REQUEST_COMPLETED
    ][-1]
    assert response.status_code == 200
    assert event.scenario_key == "repair_service"
    assert event.profile_fingerprint == "b" * 64


def test_authentication_events_are_safe_and_distinguish_success_from_rejection() -> None:
    observer = RecordingObserver()
    app = FastAPI()
    app.state.observer = observer
    app.add_middleware(ObservabilityMiddleware, observer=observer)
    tenant = Tenant(id=uuid4(), slug="acme", name="Acme", status="active")

    async def session_override():
        yield _Session(tenant)

    app.dependency_overrides[get_db_session] = session_override

    @app.get("/tenant")
    async def tenant_route(current: Tenant = Depends(get_current_tenant)) -> dict[str, str]:
        return {"tenant_id": str(current.id)}

    client = TestClient(app)
    raw_key = "ik_" + "A" * 43
    success = client.get("/tenant", headers={"X-API-Key": raw_key})
    rejected = client.get("/tenant", headers={"X-API-Key": "invalid"})

    auth_events = [event for event in observer.events if event.event is EventName.AUTH_COMPLETED]
    assert success.status_code == 200
    assert rejected.status_code == 401
    assert [event.outcome for event in auth_events] == [
        OutcomeCode.AUTHENTICATED,
        OutcomeCode.REJECTED,
    ]
    assert all(raw_key not in event.model_dump_json() for event in auth_events)


def test_signature_outcomes_remain_closed_and_public_helper_stays_boolean() -> None:
    raw_key = "ik_" + "A" * 43
    body = b"{}"
    timestamp = 1_700_000_000
    valid = sign_inbound_webhook(raw_key, timestamp, body)

    assert inbound_signature_outcome(raw_key, str(timestamp), valid, body, now=timestamp) is InboundSignatureOutcome.VERIFIED
    assert inbound_signature_outcome(raw_key, None, valid, body, now=timestamp) is InboundSignatureOutcome.MISSING
    assert inbound_signature_outcome(raw_key, "bad", valid, body, now=timestamp) is InboundSignatureOutcome.MALFORMED
    assert inbound_signature_outcome(raw_key, str(timestamp - 301), valid, body, now=timestamp) is InboundSignatureOutcome.STALE
    assert inbound_signature_outcome(raw_key, str(timestamp), "v1=" + "0" * 64, body, now=timestamp) is InboundSignatureOutcome.MISMATCH


def test_webhook_signature_dependency_emits_safe_outcome() -> None:
    observer = RecordingObserver()
    app = FastAPI()
    app.state.observer = observer
    app.add_middleware(ObservabilityMiddleware, observer=observer)

    @app.post("/webhook")
    async def webhook(current: Tenant = Depends(get_current_inbound_tenant)) -> dict[str, str]:
        return {"tenant_id": str(current.id)}

    async def session_override():
        yield _Session(None)

    app.dependency_overrides[get_db_session] = session_override
    response = TestClient(app).post(
        "/webhook",
        content=b"{}",
        headers={
            "X-API-Key": "ik_" + "A" * 43,
            "X-Inbound-Timestamp": str(int(time.time())),
            "X-Inbound-Signature": "v1=" + "0" * 64,
        },
    )

    events = [
        event
        for event in observer.events
        if event.event is EventName.INBOUND_SIGNATURE_COMPLETED
    ]
    assert response.status_code == 401
    assert len(events) == 1
    assert events[0].outcome is OutcomeCode.MISMATCH
