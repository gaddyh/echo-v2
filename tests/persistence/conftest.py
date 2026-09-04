"""Shared fixtures for Postgres persistence tests.

Spins up one PostgreSQL Testcontainer per pytest session, applies Alembic
migrations to head, and supplies independent session factories for tests.

Local vs CI behavior (per the approved plan):
* If Docker is unavailable locally, Postgres-dependent tests **skip** with a
  clear reason — existing unit tests remain runnable without Docker.
* In CI (``CI=true``), a failure to start the container or run migrations
  **fails the suite** rather than silently skipping. Silent skips in CI would
  hide regressions in the very persistence layer this milestone adds.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Make the repo root importable when pytest runs from tests/persistence/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from echo_v2.persistence.credential_cipher import IdentityCredentialCipher
from echo_v2.persistence.db import run_migrations
from echo_v2.persistence.postgres_idempotency import (
    PostgresIdempotencyStore,
)
from echo_v2.persistence.postgres_webhook_dedup import (
    PostgresWebhookDedupStore,
)
from echo_v2.persistence.postgres_whatsapp_connections import (
    PostgresWhatsAppConnectionRepository,
)
from echo_v2.persistence.unit_of_work import PostgresUnitOfWork

_IS_CI = os.environ.get("CI", "").lower() in ("1", "true", "yes")
_DOCKER_AVAILABLE = shutil.which("docker") is not None


def _docker_daemon_running() -> bool:
    """Check the Docker daemon is actually running, not just installed."""
    import subprocess

    if not _DOCKER_AVAILABLE:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001 — any failure means Docker is unavailable
        return False


def _skip_or_fail(reason: str) -> None:
    """Skip locally, fail in CI."""
    if _IS_CI:
        pytest.fail(f"Postgres persistence tests cannot run in CI: {reason}", allow_module_level=True)
    pytest.skip(reason, allow_module_level=True)


# --- session-scoped container + engine --------------------------------------

@pytest.fixture(scope="session")
def postgres_url() -> str:
    """Start a Postgres Testcontainer and return its async URL.

    Skips (locally) or fails (in CI) if Docker is unavailable.
    """
    if not _docker_daemon_running():
        _skip_or_fail("Docker daemon not available — skipping Postgres tests locally.")
        return ""  # unreachable, but keeps mypy happy

    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16-alpine", driver="psycopg")
    try:
        container.start()
    except Exception as exc:  # noqa: BLE001 — any container failure is a skip/fail
        _skip_or_fail(f"Failed to start Postgres Testcontainer: {exc}")
        return ""  # unreachable

    # Use the container's JDBC-style URL but rewrite to SQLAlchemy async form.
    # PostgresContainer.get_connection_url() returns postgresql+psycopg://...
    sync_url = container.get_connection_url()
    # The async engine uses the same psycopg URL; create_async_engine handles it.
    async_url = sync_url

    # Apply migrations once, synchronously, against the sync URL.
    try:
        run_migrations(sync_url)
    except Exception as exc:  # noqa: BLE001 — any migration failure is a skip/fail
        container.stop()
        _skip_or_fail(f"Alembic migrations failed: {exc}")
        return ""  # unreachable

    yield async_url

    container.stop()


@pytest_asyncio.fixture(scope="session")
async def engine(postgres_url: str):
    """Session-scoped async engine bound to the testcontainer."""
    eng = create_async_engine(postgres_url, pool_pre_ping=True)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    """Per-test session factory. Each test gets fresh sessions."""
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def clean_db(engine) -> AsyncIterator[None]:
    """Truncate all foundation tables before each test for isolation."""
    async with engine.begin() as conn:
        # Order matters: respect FK constraints (children first).
        await conn.exec_driver_sql(
            "TRUNCATE TABLE "
            "scheduled_actions, "
            "idempotency_operations, "
            "provider_webhook_events, "
            "whatsapp_connections, "
            "users "
            "RESTART IDENTITY CASCADE"
        )
    yield


# --- repository fixtures ----------------------------------------------------

@pytest_asyncio.fixture
async def connections_repo(session_factory, clean_db) -> PostgresWhatsAppConnectionRepository:
    return PostgresWhatsAppConnectionRepository(session_factory, IdentityCredentialCipher())


@pytest_asyncio.fixture
async def webhooks_repo(session_factory, clean_db) -> PostgresWebhookDedupStore:
    return PostgresWebhookDedupStore(session_factory)


@pytest_asyncio.fixture
async def idempotency_repo(session_factory, clean_db) -> PostgresIdempotencyStore:
    return PostgresIdempotencyStore(session_factory, lease_seconds=2)


@pytest_asyncio.fixture
async def unit_of_work_factory(session_factory, clean_db):
    def _make() -> PostgresUnitOfWork:
        return PostgresUnitOfWork(session_factory, IdentityCredentialCipher(), lease_seconds=2)
    return _make


@pytest_asyncio.fixture
async def scheduled_actions_repo(session_factory, clean_db) -> PostgresScheduledActionRepository:
    from echo_v2.persistence.postgres_scheduled_actions import (
        PostgresScheduledActionRepository,
    )
    return PostgresScheduledActionRepository(session_factory)


# --- user helper ------------------------------------------------------------

async def insert_user(session_factory, phone: str = "+972546610653") -> str:
    """Insert a user row and return its id (UUID string)."""
    from sqlalchemy import text

    async with session_factory() as session:
        result = await session.execute(
            text("INSERT INTO users (phone_number) VALUES (:p) RETURNING id"),
            {"p": phone},
        )
        user_id = str(result.scalar_one())
        await session.commit()
    return user_id
