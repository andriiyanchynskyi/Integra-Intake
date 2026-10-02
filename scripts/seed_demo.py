"""Provision a local demo tenant and print its newly rotated API key once."""

from __future__ import annotations

import asyncio
import argparse
import sys
from collections.abc import Callable
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import generate_api_key
from app.core.config import settings
from app.db.models import ApiKey, Tenant
from app.db.session import async_session_factory
from app.runtime.profiles import TenantProfileResolver, TenantProfileUnavailableError


DEMO_PROFILE_SLUGS = (
    "freight-broker",
    "repair-service",
    "language-school",
)
DEMO_TENANT_SLUG = DEMO_PROFILE_SLUGS[0]


def resolve_profile_slug(profile: str) -> str:
    """Validate and return the slug of one trusted checked-in profile."""

    try:
        return TenantProfileResolver(settings.tenant_profiles_directory).resolve(profile).config.slug
    except TenantProfileUnavailableError as error:
        raise ValueError("trusted tenant profile is unavailable") from error


async def seed_demo_tenant(session: AsyncSession, slug: str = DEMO_TENANT_SLUG) -> str:
    """Create the demo tenant by slug if needed and intentionally rotate its API key."""
    async with session.begin():
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(slug=slug, name="Demo tenant", status="active")
            session.add(tenant)
            await session.flush()
        else:
            tenant.status = "active"

        active_keys = (
            await session.execute(
                select(ApiKey).where(
                    ApiKey.tenant_id == tenant.id,
                    ApiKey.is_active.is_(True),
                    ApiKey.principal_type == "service",
                )
            )
        ).scalars().all()
        for active_key in active_keys:
            active_key.is_active = False

        raw_key, prefix, key_hash = generate_api_key()
        session.add(
            ApiKey(tenant_id=tenant.id, prefix=prefix, key_hash=key_hash, is_active=True)
        )

    return raw_key


async def seed_operator_key(
    session: AsyncSession,
    actor_ref: str,
    slug: str = DEMO_TENANT_SLUG,
) -> str:
    """Provision one approval-decider key and return its raw value once."""

    normalized_actor_ref = actor_ref.strip()
    if not normalized_actor_ref or len(normalized_actor_ref) > 255:
        raise ValueError("operator actor_ref must be non-blank and at most 255 characters")
    async with session.begin():
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none()
        if tenant is None:
            raise ValueError("tenant must be seeded before an operator key")
        if tenant.status != "active":
            raise ValueError("tenant must be active before an operator key")
        active_operator_keys = (
            await session.execute(
                select(ApiKey).where(
                    ApiKey.tenant_id == tenant.id,
                    ApiKey.is_active.is_(True),
                    ApiKey.principal_type == "operator",
                )
            )
        ).scalars().all()
        for active_key in active_operator_keys:
            active_key.is_active = False

        raw_key, prefix, key_hash = generate_api_key()
        session.add(
            ApiKey(
                tenant_id=tenant.id,
                prefix=prefix,
                key_hash=key_hash,
                is_active=True,
                principal_type="operator",
                capability="approval_decider",
                actor_ref=normalized_actor_ref,
            )
        )
    return raw_key


async def main(
    output: Callable[[str], None] = print,
    *,
    operator_ref: str | None = None,
    profile: str = DEMO_TENANT_SLUG,
) -> None:
    slug = resolve_profile_slug(profile)
    async with async_session_factory() as session:
        if operator_ref is None:
            raw_key = await seed_demo_tenant(session, slug)
        else:
            raw_key = await seed_operator_key(session, operator_ref, slug)
    output(raw_key)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-ref", help="provision an approval-decider key")
    parser.add_argument(
        "--profile",
        default=DEMO_TENANT_SLUG,
        choices=DEMO_PROFILE_SLUGS,
        help="trusted tenant profile slug",
    )
    arguments = parser.parse_args()
    asyncio.run(main(operator_ref=arguments.operator_ref, profile=arguments.profile))
