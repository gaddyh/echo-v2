"""Postgres-backed persistence tests for ops digests.

Starts a Postgres Testcontainer, runs migrations, and verifies record
idempotency (digest_key conflict), list_since ordering and latest().

Requires Docker. Skips locally if Docker is unavailable; fails in CI.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.ops_digest import OpsDigest
from echo_v2.persistence.orm import OpsDigestRow
from echo_v2.persistence.postgres_ops_digests import PostgresOpsDigestRepository

pytestmark = pytest.mark.asyncio

NOW = datetime.now(timezone.utc)


def _digest(key: str, *, minutes_ago: float = 0, one_liner: str = "quiet") -> OpsDigest:
    return OpsDigest(
        digest_key=key,
        worker_id="worker-a",
        source="connector",
        severity="info",
        attention=False,
        one_liner=one_liner,
        digest={"one_liner": one_liner, "still_open": [], "happened": []},
        received_at=NOW - timedelta(minutes=minutes_ago),
    )


@pytest_asyncio.fixture
async def repo(
    session_factory: async_sessionmaker[AsyncSession], clean_db: None
) -> PostgresOpsDigestRepository:
    return PostgresOpsDigestRepository(session_factory)


async def test_record_and_list_since(repo: PostgresOpsDigestRepository) -> None:
    await repo.record(_digest("k-old", minutes_ago=60, one_liner="older"))
    await repo.record(_digest("k-new", minutes_ago=5, one_liner="newer"))

    recent = await repo.list_since(since=NOW - timedelta(minutes=30))
    assert [d.digest_key for d in recent] == ["k-new"]

    older = await repo.list_since(since=NOW - timedelta(hours=2))
    assert [d.digest_key for d in older] == ["k-new", "k-old"]
    assert older[1].one_liner == "older"


async def test_record_is_idempotent_on_digest_key(
    repo: PostgresOpsDigestRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await repo.record(_digest("k-dup", one_liner="first"))
    await repo.record(_digest("k-dup", one_liner="second"))

    async with session_factory() as session:
        rows = await session.execute(
            select(func.count()).select_from(OpsDigestRow)
        )
        assert rows.scalar_one() == 1

    latest = await repo.latest()
    assert latest is not None
    assert latest.digest_key == "k-dup"
    assert latest.one_liner == "first"


async def test_latest_returns_none_when_empty(repo: PostgresOpsDigestRepository) -> None:
    assert await repo.latest() is None
