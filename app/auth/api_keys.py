"""API-key provisioning and request authentication.

Credential format: ``ik_`` followed by the 43 URL-safe characters emitted by
``secrets.token_urlsafe(32)``. ``generate_api_key`` returns the raw credential
only to its provisioning caller, together with display-safe prefix metadata and
the digest to persist. Authentication accepts that exact format, returns only a
Tenant, and must never log or include the raw credential in an HTTP response.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiKey, Tenant
from app.db.session import get_db_session


API_KEY_PREFIX = "ik_"
API_KEY_TOKEN_BYTES = 32
API_KEY_TOKEN_LENGTH = 43
API_KEY_FORMAT = "ik_<43 URL-safe characters from secrets.token_urlsafe(32)>"
API_KEY_TOKEN_PATTERN = re.compile(
    rf"{re.escape(API_KEY_PREFIX)}[A-Za-z0-9_-]{{{API_KEY_TOKEN_LENGTH}}}\Z"
)
SAFE_PREFIX_LENGTH = 11


@dataclass(frozen=True, slots=True)
class AuthenticatedOperator:
    """Tenant-scoped, non-secret operator identity for approval decisions."""

    tenant_id: UUID
    actor_ref: str
    credential_id: UUID


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    raw_key = f"{API_KEY_PREFIX}{secrets.token_urlsafe(API_KEY_TOKEN_BYTES)}"
    return raw_key, raw_key[:SAFE_PREFIX_LENGTH], hash_api_key(raw_key)


async def get_current_tenant(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    session: AsyncSession = Depends(get_db_session),
) -> Tenant:
    if x_api_key is None or not API_KEY_TOKEN_PATTERN.fullmatch(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    statement = (
        select(Tenant)
        .join(ApiKey, ApiKey.tenant_id == Tenant.id)
        .where(ApiKey.key_hash == hash_api_key(x_api_key), ApiKey.is_active.is_(True))
    )
    tenant = (await session.execute(statement)).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return tenant


async def get_current_operator(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    session: AsyncSession = Depends(get_db_session),
) -> AuthenticatedOperator:
    """Authenticate only active operator credentials allowed to decide approvals."""

    if x_api_key is None or not API_KEY_TOKEN_PATTERN.fullmatch(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid operator API key",
        )

    statement = (
        select(ApiKey)
        .join(Tenant, ApiKey.tenant_id == Tenant.id)
        .where(
            ApiKey.key_hash == hash_api_key(x_api_key),
            ApiKey.is_active.is_(True),
            ApiKey.principal_type == "operator",
            ApiKey.capability == "approval_decider",
            ApiKey.actor_ref.is_not(None),
            Tenant.status == "active",
        )
    )
    credential = (await session.execute(statement)).scalar_one_or_none()
    if credential is None or not credential.actor_ref:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid operator API key",
        )
    return AuthenticatedOperator(
        tenant_id=credential.tenant_id,
        actor_ref=credential.actor_ref,
        credential_id=credential.id,
    )
