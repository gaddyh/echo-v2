"""Postgres-backed DailyDigestRepository."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.digest import DailyDigest, DailyDigestStatus
from echo_v2.persistence.orm import DailyDigestRow

__all__ = ["PostgresDailyDigestRepository"]


class PostgresDailyDigestRepository:
    """PostgreSQL implementation of :class:`DailyDigestRepository`.

    Uses ``INSERT ... ON CONFLICT DO NOTHING`` for atomic claim semantics.
    If the insert returns 0 rows, a digest already exists for this
    ``(user_id, local_date)`` — return ``None`` to signal "already claimed".
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def claim_or_get(
        self,
        *,
        user_id: str,
        local_date: date,
    ) -> DailyDigest | None:
        async with self._session_factory() as session:
            stmt = (
                pg_insert(DailyDigestRow)
                .values(
                    user_id=user_id,
                    local_date=local_date,
                    status=DailyDigestStatus.PROCESSING.value,
                )
                .on_conflict_do_nothing(
                    index_elements=["user_id", "local_date"],
                )
                .returning(DailyDigestRow)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                # Already exists — already claimed
                await session.rollback()
                return None
            await session.commit()
            return self._row_to_domain(row)

    async def update_status(
        self,
        *,
        digest_id: str,
        status: DailyDigestStatus,
        sent_at: datetime | None = None,
        provider_message_id: str | None = None,
        item_count: int = 0,
    ) -> bool:
        from sqlalchemy import update

        async with self._session_factory() as session:
            stmt = (
                update(DailyDigestRow)
                .where(DailyDigestRow.id == digest_id)
                .values(
                    status=status.value,
                    sent_at=sent_at,
                    provider_message_id=provider_message_id,
                    item_count=item_count,
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

    async def get(
        self,
        *,
        user_id: str,
        local_date: date,
    ) -> DailyDigest | None:
        async with self._session_factory() as session:
            stmt = select(DailyDigestRow).where(
                DailyDigestRow.user_id == user_id,
                DailyDigestRow.local_date == local_date,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_domain(row)

    @staticmethod
    def _row_to_domain(row: DailyDigestRow) -> DailyDigest:
        return DailyDigest(
            id=str(row.id),
            user_id=str(row.user_id),
            local_date=row.local_date,
            status=DailyDigestStatus(row.status),
            created_at=row.created_at,
            sent_at=row.sent_at,
            provider_message_id=row.provider_message_id,
            item_count=row.item_count,
        )
