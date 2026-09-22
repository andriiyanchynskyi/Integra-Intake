"""Provision a local demo tenant and print its newly rotated API key once."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import generate_api_key
from app.db.models import ApiKey, Tenant
from app.db.session import async_session_factory


DEMO_TENANT_SLUG = "freight-broker"


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

        active_keys = (
            await session.execute(
                select(ApiKey).where(ApiKey.tenant_id == tenant.id, ApiKey.is_active.is_(True))
            )
        ).scalars().all()
        for active_key in active_keys:
            active_key.is_active = False

        raw_key, prefix, key_hash = generate_api_key()
        session.add(
            ApiKey(tenant_id=tenant.id, prefix=prefix, key_hash=key_hash, is_active=True)
        )

    return raw_key


async def main(output: Callable[[str], None] = print) -> None:
    async with async_session_factory() as session:
        raw_key = await seed_demo_tenant(session)
    output(raw_key)


if __name__ == "__main__":
    asyncio.run(main())
