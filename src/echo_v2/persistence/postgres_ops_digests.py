"""PostgreSQL repository for connector ops digests."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.ops_digest import OpsDigest
from echo_v2.persistence.orm import OpsDigestRow


def _to_domain(row: OpsDigestRow) -> OpsDigest:
    return OpsDigest(
        digest_key=row.digest_key,
        worker_id=row.worker_id,
        source=row.source,
        severity=row.severity,
        attention=row.attention,
        one_liner=row.one_liner,
        digest=row.digest,
        metrics=row.metrics,
        window_from=row.window_from,
        window_to=row.window_to,
        received_at=row.received_at,
    )


class PostgresOpsDigestRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, digest: OpsDigest) -> None:
        async with self._session_factory() as session:
            await session.execute(
                pg_insert(OpsDigestRow)
                .values(
                    digest_key=digest.digest_key,
                    worker_id=digest.worker_id,
                    source=digest.source,
                    severity=digest.severity,
                    attention=digest.attention,
                    one_liner=digest.one_liner,
                    digest=digest.digest,
                    metrics=digest.metrics,
                    window_from=digest.window_from,
                    window_to=digest.window_to,
                    received_at=digest.received_at or datetime.now(timezone.utc),
                )
                .on_conflict_do_nothing(index_elements=["digest_key"])
            )
            await session.commit()

    async def list_since(
        self,
        *,
        since: datetime,
        limit: int = 100,
    ) -> list[OpsDigest]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(OpsDigestRow)
                .where(OpsDigestRow.received_at >= since)
                .order_by(OpsDigestRow.received_at.desc())
                .limit(limit)
            )
            return [_to_domain(row) for row in rows.scalars()]

    async def latest(self) -> OpsDigest | None:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(OpsDigestRow)
                .order_by(OpsDigestRow.received_at.desc())
                .limit(1)
            )
            row = rows.scalars().first()
            return _to_domain(row) if row is not None else None
