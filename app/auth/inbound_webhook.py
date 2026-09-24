"""Authentication primitives for the signed email-like inbound webhook."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_keys import (
    API_KEY_TOKEN_PATTERN,
    _resolve_active_tenant,
)
from app.db.models import Tenant
from app.db.session import get_db_session
from app.inbound.models import MAX_INBOUND_WEBHOOK_BODY_BYTES


SIGNATURE_VERSION = "v1"
MAX_SIGNATURE_AGE_SECONDS = 300


def signature_payload(timestamp: int, body: bytes) -> bytes:
    return b"v1." + str(timestamp).encode("ascii") + b"." + body


def sign_inbound_webhook(api_key: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(
        api_key.encode("utf-8"),
        signature_payload(timestamp, body),
        hashlib.sha256,
    ).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def verify_inbound_webhook_signature(
    api_key: str,
    timestamp_header: str | None,
    signature: str | None,
    body: bytes,
    *,
    now: int | None = None,
) -> bool:
    if timestamp_header is None or signature is None:
        return False
    if not signature.isascii():
        return False
    try:
        timestamp = int(timestamp_header)
    except (TypeError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > MAX_SIGNATURE_AGE_SECONDS:
        return False
    expected = sign_inbound_webhook(api_key, timestamp, body)
    try:
        return hmac.compare_digest(expected, signature)
    except TypeError:
        return False


def _invalid_webhook_authentication() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid inbound webhook",
    )


async def read_bounded_inbound_body(request: Request) -> bytes:
    """Read and cache the raw webhook body without unbounded buffering."""

    state_key = "_integra_inbound_raw_body"
    if hasattr(request.state, state_key):
        return getattr(request.state, state_key)

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = 0
        if declared_length > MAX_INBOUND_WEBHOOK_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Inbound webhook body too large",
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_INBOUND_WEBHOOK_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Inbound webhook body too large",
            )
        chunks.append(chunk)

    body = b"".join(chunks)
    setattr(request.state, state_key, body)
    return body


async def get_current_inbound_tenant(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    x_inbound_timestamp: Annotated[
        str | None, Header(alias="X-Inbound-Timestamp")
    ] = None,
    x_inbound_signature: Annotated[
        str | None, Header(alias="X-Inbound-Signature")
    ] = None,
    session: AsyncSession = Depends(get_db_session),
) -> Tenant:
    if x_api_key is None or not API_KEY_TOKEN_PATTERN.fullmatch(x_api_key):
        raise _invalid_webhook_authentication()

    body = await read_bounded_inbound_body(request)
    if not verify_inbound_webhook_signature(
        x_api_key,
        x_inbound_timestamp,
        x_inbound_signature,
        body,
    ):
        raise _invalid_webhook_authentication()

    tenant = await _resolve_active_tenant(x_api_key, session)
    if tenant is None:
        raise _invalid_webhook_authentication()
    return tenant


__all__ = [
    "MAX_SIGNATURE_AGE_SECONDS",
    "SIGNATURE_VERSION",
    "get_current_inbound_tenant",
    "read_bounded_inbound_body",
    "sign_inbound_webhook",
    "signature_payload",
    "verify_inbound_webhook_signature",
]
