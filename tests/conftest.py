"""Shared PostgreSQL fixtures for integration tests.

These fixtures deliberately require an explicit ``DATABASE_URL``.  They never
silently fall back to the application's local-development default, so a unit
test run cannot be mistaken for live PostgreSQL evidence.
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_db_session
from app.main import app


PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def _verify_database_connection(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def migrated_postgres_url() -> str:
    """Return a reachable database URL after applying the Alembic head revision."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("PostgreSQL integration skipped: DATABASE_URL is not set.")

    try:
        asyncio.run(_verify_database_connection(database_url))
    except Exception as error:  # Connection failures vary across asyncpg/platform versions.
        pytest.skip(
            "PostgreSQL integration skipped: DATABASE_URL is unreachable "
            f"({type(error).__name__}: {error})."
        )

    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert migration.returncode == 0, migration.stderr
    return database_url


@pytest.fixture
async def postgres_connection(migrated_postgres_url: str) -> AsyncGenerator[AsyncConnection, None]:
    """Provide a per-test outer transaction that is rolled back after the test."""
    engine = create_async_engine(migrated_postgres_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                yield connection
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def postgres_session_factory(
    postgres_connection: AsyncConnection,
) -> async_sessionmaker[AsyncSession]:
    """Create sessions that use savepoints inside the test's outer transaction."""
    return async_sessionmaker(
        bind=postgres_connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@pytest.fixture
async def postgres_session(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async with postgres_session_factory() as session:
        yield session


@pytest.fixture
async def postgres_client(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Exercise the ASGI app with real async PostgreSQL sessions."""
    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with postgres_session_factory() as session:
            try:
                yield session
                # Several FastAPI dependencies receive separate sessions bound to
                # one test connection. Releasing a read-only authentication
                # savepoint preserves writes committed by the case-service
                # savepoint; the fixture's outer transaction still rolls all
                # data back after the test.
                if session.in_transaction():
                    await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_session] = override_session
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.clear()
