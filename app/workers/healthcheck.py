"""Minimal worker readiness probe with bounded database access."""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings


async def _check() -> bool:
    engine = create_async_engine(
        settings.database_url,
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.database_statement_timeout_ms),
                "lock_timeout": str(settings.database_lock_timeout_ms),
            }
        },
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    finally:
        await engine.dispose()


def main() -> int:
    try:
        return 0 if asyncio.run(_check()) else 1
    except BaseException:
        return 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]
