"""PostgreSQL implementation of the GuardDailyStatsRepository.

Uses ``INSERT ... ON CONFLICT (child_user_id, local_date) DO UPDATE`` for
idempotent upserts — rebuilding a day yields the same row without inflation.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.future import select

from echo_v2.domain.guard_daily_stats import GuardDailyStats
from echo_v2.persistence.orm import GuardDailyStatsRow

__all__ = ["PostgresGuardDailyStatsRepository"]


class PostgresGuardDailyStatsRepository:
    """Postgres-backed daily stats repository with idempotent upsert."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def upsert(self, stats: GuardDailyStats) -> None:
        """Insert or update a daily stats row (idempotent by child+date)."""
        row = _to_row(stats)
        async with self._session_factory() as session:
            stmt = pg_insert(GuardDailyStatsRow).values(**row)
            stmt = stmt.on_conflict_do_update(
                constraint="guard_daily_stats_child_date_key",
                set_={
                    col: getattr(stmt.excluded, col)
                    for col in row
                    if col not in ("child_user_id", "local_date")
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def get(
        self, child_user_id: str, local_date: date
    ) -> GuardDailyStats | None:
        """Fetch a single day's stats for a child."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(GuardDailyStatsRow).where(
                    GuardDailyStatsRow.child_user_id == child_user_id,
                    GuardDailyStatsRow.local_date == local_date,
                )
            )
            row = result.scalar_one_or_none()
            return _to_domain(row) if row is not None else None

    async def list_for_range(
        self,
        *,
        child_user_id: str | None = None,
        date_from: date,
        date_to: date,
    ) -> list[GuardDailyStats]:
        """List daily stats in a date range [date_from, date_to]."""
        async with self._session_factory() as session:
            stmt = select(GuardDailyStatsRow).where(
                GuardDailyStatsRow.local_date >= date_from,
                GuardDailyStatsRow.local_date <= date_to,
            )
            if child_user_id is not None:
                stmt = stmt.where(GuardDailyStatsRow.child_user_id == child_user_id)
            stmt = stmt.order_by(GuardDailyStatsRow.local_date)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_to_domain(row) for row in rows]


def _to_row(stats: GuardDailyStats) -> dict[str, Any]:
    """Convert a domain GuardDailyStats to a row dict for insert."""
    return {
        "child_user_id": stats.child_user_id,
        "local_date": stats.local_date,
        "timezone": stats.timezone,
        "messages_received": stats.messages_received,
        "private_messages": stats.private_messages,
        "group_messages": stats.group_messages,
        "active_private_chats": stats.active_private_chats,
        "active_group_chats": stats.active_group_chats,
        "images_received": stats.images_received,
        "videos_received": stats.videos_received,
        "audio_received": stats.audio_received,
        "links_received": stats.links_received,
        "documents_received": stats.documents_received,
        "images_enriched": stats.images_enriched,
        "videos_enriched": stats.videos_enriched,
        "audio_enriched": stats.audio_enriched,
        "links_enriched": stats.links_enriched,
        "documents_enriched": stats.documents_enriched,
        "guard_assessments": stats.guard_assessments,
        "security_detections": stats.security_detections,
        "exposure_detections": stats.exposure_detections,
        "security_alerts_sent": stats.security_alerts_sent,
        "exposure_alerts_sent": stats.exposure_alerts_sent,
        "alerts_failed": stats.alerts_failed,
        "analysis_failures": stats.analysis_failures,
        "media_enrichment_failures": stats.media_enrichment_failures,
        "coverage_health": stats.coverage_health,
        "security_attention": stats.security_attention,
        "coverage_status": stats.coverage_status,
        "connection_seconds": stats.connection_seconds,
        "updated_at": stats.updated_at,
    }


def _to_domain(row: GuardDailyStatsRow) -> GuardDailyStats:
    """Convert an ORM row to a domain GuardDailyStats."""
    return GuardDailyStats(
        child_user_id=str(row.child_user_id),
        local_date=row.local_date,
        timezone=row.timezone,
        messages_received=row.messages_received,
        private_messages=row.private_messages,
        group_messages=row.group_messages,
        active_private_chats=row.active_private_chats,
        active_group_chats=row.active_group_chats,
        images_received=row.images_received,
        videos_received=row.videos_received,
        audio_received=row.audio_received,
        links_received=row.links_received,
        documents_received=row.documents_received,
        images_enriched=row.images_enriched,
        videos_enriched=row.videos_enriched,
        audio_enriched=row.audio_enriched,
        links_enriched=row.links_enriched,
        documents_enriched=row.documents_enriched,
        guard_assessments=row.guard_assessments,
        security_detections=row.security_detections,
        exposure_detections=row.exposure_detections,
        security_alerts_sent=row.security_alerts_sent,
        exposure_alerts_sent=row.exposure_alerts_sent,
        alerts_failed=row.alerts_failed,
        analysis_failures=row.analysis_failures,
        media_enrichment_failures=row.media_enrichment_failures,
        coverage_health=row.coverage_health,
        security_attention=row.security_attention,
        coverage_status=row.coverage_status,
        connection_seconds=row.connection_seconds,
        updated_at=row.updated_at or datetime.now(timezone.utc),
    )
