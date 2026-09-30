"""Authentication primitives for the signed email-like inbound webhook."""

from __future__ import annotations

import hashlib
import hmac
from enum import Enum
import re
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
from app.observability import (
    Component,
    CredentialKind,
    EventName,
    ObservationEvent,
    ObservationContext,
    OutcomeCode,
    request_context,
    request_observer,
    safe_emit,
)


SIGNATURE_VERSION = "v1"
MAX_SIGNATURE_AGE_SECONDS = 300
SIGNATURE_DIGEST_PATTERN = re.compile(r"^v1=[0-9a-f]{64}$")


class InboundSignatureOutcome(str, Enum):
    VERIFIED = "verified"
    MISSING = "missing"
    MALFORMED = "malformed"
    STALE = "stale"
    MISMATCH = "mismatch"


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
    return (
        inbound_signature_outcome(
            api_key,
            timestamp_header,
            signature,
            body,
            now=now,
        )
        is InboundSignatureOutcome.VERIFIED
    )


def inbound_signature_outcome(
    api_key: str,
    timestamp_header: str | None,
    signature: str | None,
    body: bytes,
    *,
    now: int | None = None,
) -> InboundSignatureOutcome:
    if timestamp_header is None or signature is None:
        return InboundSignatureOutcome.MISSING
    if not isinstance(body, bytes) or not signature.isascii():
        return InboundSignatureOutcome.MALFORMED
    if SIGNATURE_DIGEST_PATTERN.fullmatch(signature) is None:
        return InboundSignatureOutcome.MALFORMED
    try:
        timestamp = int(timestamp_header)
    except (TypeError, ValueError):
        return InboundSignatureOutcome.MALFORMED
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > MAX_SIGNATURE_AGE_SECONDS:
        return InboundSignatureOutcome.STALE
    expected = sign_inbound_webhook(api_key, timestamp, body)
    try:
        verified = hmac.compare_digest(expected, signature)
    except TypeError:
        return InboundSignatureOutcome.MALFORMED
    return (
        InboundSignatureOutcome.VERIFIED
        if verified
        else InboundSignatureOutcome.MISMATCH
    )


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
        _emit_signature(request, InboundSignatureOutcome.MALFORMED)
        _emit_webhook_auth(request, OutcomeCode.REJECTED)
        raise _invalid_webhook_authentication()

    body = await read_bounded_inbound_body(request)
    signature_outcome = inbound_signature_outcome(
        x_api_key,
        x_inbound_timestamp,
        x_inbound_signature,
        body,
    )
    _emit_signature(request, signature_outcome)
    if signature_outcome is not InboundSignatureOutcome.VERIFIED:
        _emit_webhook_auth(request, OutcomeCode.REJECTED)
        raise _invalid_webhook_authentication()

    tenant = await _resolve_active_tenant(x_api_key, session)
    if tenant is None:
        _emit_webhook_auth(request, OutcomeCode.REJECTED)
        raise _invalid_webhook_authentication()
    _emit_webhook_auth(request, OutcomeCode.AUTHENTICATED, tenant_id=tenant.id)
    return tenant


def _emit_signature(request: Request, outcome: InboundSignatureOutcome) -> None:
    try:
        context: ObservationContext = request_context(request)
    except RuntimeError:
        return
    safe_emit(
        request_observer(request),
        ObservationEvent(
            event=EventName.INBOUND_SIGNATURE_COMPLETED,
            trace_id=context.trace_id,
            request_id=context.request_id,
            tenant_id=context.tenant_id,
            component=Component.AUTH,
            outcome=OutcomeCode(outcome.value),
            credential_kind=CredentialKind.INBOUND_WEBHOOK,
        ),
    )


def _emit_webhook_auth(
    request: Request,
    outcome: OutcomeCode,
    *,
    tenant_id: object | None = None,
) -> None:
    try:
        context: ObservationContext = request_context(request)
    except RuntimeError:
        return
    safe_emit(
        request_observer(request),
        ObservationEvent(
            event=EventName.AUTH_COMPLETED,
            trace_id=context.trace_id,
            request_id=context.request_id,
            tenant_id=tenant_id,
            component=Component.AUTH,
            outcome=outcome,
            credential_kind=CredentialKind.INBOUND_WEBHOOK,
        ),
    )


__all__ = [
    "MAX_SIGNATURE_AGE_SECONDS",
    "InboundSignatureOutcome",
    "SIGNATURE_VERSION",
    "get_current_inbound_tenant",
    "inbound_signature_outcome",
    "read_bounded_inbound_body",
    "sign_inbound_webhook",
    "signature_payload",
    "verify_inbound_webhook_signature",
]
