from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.body_limit import MAX_API_REQUEST_BODY_BYTES, RequestBodyLimitMiddleware
from app.inbound.models import MAX_INBOUND_WEBHOOK_BODY_BYTES


def _build_app(calls: list[int]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware)

    @app.post("/v1/intake")
    async def intake(request: Request) -> dict[str, int]:
        body = await request.body()
        calls.append(len(body))
        return {"bytes": len(body)}

    @app.post("/v1/inbound/email/webhook")
    async def webhook(request: Request) -> dict[str, int]:
        body = await request.body()
        calls.append(len(body))
        return {"bytes": len(body)}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_declared_oversized_body_is_rejected_before_handler() -> None:
    calls: list[int] = []
    client = TestClient(_build_app(calls))

    response = client.post(
        "/v1/intake",
        content=b"x" * (MAX_API_REQUEST_BODY_BYTES + 1),
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert calls == []


def test_chunked_body_crossing_limit_is_rejected_before_handler() -> None:
    calls: list[int] = []
    client = TestClient(_build_app(calls))

    def chunks():
        yield b"x" * (MAX_API_REQUEST_BODY_BYTES // 2)
        yield b"y" * (MAX_API_REQUEST_BODY_BYTES // 2 + 1)

    response = client.post("/v1/intake", content=chunks())

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert calls == []


def test_exact_api_limit_reaches_handler() -> None:
    calls: list[int] = []
    client = TestClient(_build_app(calls))

    response = client.post(
        "/v1/intake",
        content=b"x" * MAX_API_REQUEST_BODY_BYTES,
    )

    assert response.status_code == 200
    assert response.json() == {"bytes": MAX_API_REQUEST_BODY_BYTES}
    assert calls == [MAX_API_REQUEST_BODY_BYTES]


def test_webhook_uses_its_larger_existing_limit() -> None:
    calls: list[int] = []
    client = TestClient(_build_app(calls))

    response = client.post(
        "/v1/inbound/email/webhook",
        content=b"x" * (MAX_API_REQUEST_BODY_BYTES + 1),
    )

    assert response.status_code == 200
    assert response.json() == {"bytes": MAX_API_REQUEST_BODY_BYTES + 1}
    assert calls == [MAX_API_REQUEST_BODY_BYTES + 1]
    assert MAX_INBOUND_WEBHOOK_BODY_BYTES > MAX_API_REQUEST_BODY_BYTES


def test_get_request_is_not_limited() -> None:
    client = TestClient(_build_app([]))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
