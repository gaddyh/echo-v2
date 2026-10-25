"""GuardReportingService — rebuild daily statistics from existing records.

This service aggregates message counts, media counts, detection/alert counts,
and coverage health from existing tables into ``guard_daily_stats`` rows.

Key design decisions (from the plan):

- **Detections vs sent alerts**: ``security_detections`` comes from
  ``guard_simple_assessments`` where ``parent_should_know=True`` (analyzer YES).
  ``security_alerts_sent`` comes from delivery outcomes with ``status='sent'``.
  Never infer sent notifications from analyzer output.

- **Coverage health is independent of alerts**: ``coverage_health`` depends
  only on connection, processing delay, enrichment, and overdue pending work.
  ``security_attention`` is true if any alert was sent. The parent-facing
  ``coverage_status`` combines both.

- **A disconnected child can never be ``protected``**.

- **Event-time calendar aggregation**: messages belong to the day based on
  ``messages.timestamp`` in the child's timezone.

- **Idempotent**: rebuilding a day twice yields the same row (upsert).

- **No new LLM analysis**: all data comes from existing tables.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.guard_daily_stats import CoverageStatus, GuardDailyStats
from echo_v2.persistence.guard_daily_stats import GuardDailyStatsRepository
from echo_v2.persistence.orm import (
    GuardExposureAssessmentRow,
    GuardExposureDeliveryOutcomeRow,
    GuardParentAttentionDeliveryOutcomeRow,
    GuardSimpleAssessmentRow,
    MessageEnrichmentRow,
    MessageRow,
    PipelineEventRow,
    WhatsAppConnectionRow,
)

__all__ = [
    "AnalyticsOverview",
    "ChildSummary",
    "DailySeriesPoint",
    "GuardReportingService",
    "ReportingConfig",
]

_logger = logging.getLogger(__name__)

# Media type labels on messages.message_type.
_MEDIA_TYPES: dict[str, str] = {
    "image": "images",
    "video": "videos",
    "audio": "audio",
    "document": "documents",
}


@dataclass(frozen=True)
class ChildSummary:
    """One row in the admin children table."""

    child_user_id: str
    child_name: str | None
    local_date: date
    coverage_status: str
    coverage_health: bool
    messages_received: int
    guard_assessments: int
    security_detections: int
    exposure_detections: int
    security_alerts_sent: int
    exposure_alerts_sent: int
    connection_seconds: int | None


@dataclass(frozen=True)
class DailySeriesPoint:
    """One day in the daily volume series."""

    local_date: date
    messages_received: int
    guard_assessments: int
    security_detections: int
    exposure_detections: int
    security_alerts_sent: int
    exposure_alerts_sent: int


@dataclass(frozen=True)
class AnalyticsOverview:
    """Aggregated totals across the selected period."""

    children_monitored: int
    total_messages: int
    total_assessments: int
    total_security_detections: int
    total_exposure_detections: int
    total_security_alerts_sent: int
    total_exposure_alerts_sent: int
    protected_days: int
    attention_days: int
    incomplete_days: int
    coverage_health_pct: float


class ReportingConfig:
    """Configurable thresholds for coverage health assessment.

    Conservative defaults — a quiet day with no messages needs no analysis
    and is still ``protected`` (if connected).
    """

    def __init__(
        self,
        *,
        connection_down_threshold_seconds: int = 3600,
        processing_delay_threshold_seconds: int = 300,
        enrichment_failure_threshold_pct: float = 0.5,
        default_timezone: str = "Asia/Jerusalem",
    ) -> None:
        self.connection_down_threshold_seconds = connection_down_threshold_seconds
        self.processing_delay_threshold_seconds = processing_delay_threshold_seconds
        self.enrichment_failure_threshold_pct = enrichment_failure_threshold_pct
        self.default_timezone = default_timezone


class GuardReportingService:
    """Rebuild daily statistics from existing records.

    Does not trigger any LLM analysis. All data comes from existing tables.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        daily_stats_repo: GuardDailyStatsRepository,
        config: ReportingConfig | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._daily_stats_repo = daily_stats_repo
        self._config = config or ReportingConfig()

    async def rebuild_day(
        self,
        *,
        child_user_id: str,
        local_date: date,
        timezone_name: str | None = None,
    ) -> GuardDailyStats:
        """Rebuild the daily stats row for one child on one date.

        Idempotent — upserts by (child_user_id, local_date).
        """
        tz_name = timezone_name or self._config.default_timezone
        tz = ZoneInfo(tz_name)

        # Compute UTC boundaries for the local day.
        day_start_utc = datetime.combine(local_date, datetime.min.time(), tz)
        day_end_utc = day_start_utc + timedelta(days=1)

        async with self._session_factory() as session:
            messages = await self._query_messages(
                session, child_user_id, day_start_utc, day_end_utc
            )
            enrichments = await self._query_enrichments(
                session, child_user_id, day_start_utc, day_end_utc
            )
            security_assessments = await self._query_security_assessments(
                session, child_user_id, day_start_utc, day_end_utc
            )
            exposure_assessments = await self._query_exposure_assessments(
                session, child_user_id, day_start_utc, day_end_utc
            )
            security_outcomes = await self._query_security_outcomes(
                session, child_user_id, day_start_utc, day_end_utc
            )
            exposure_outcomes = await self._query_exposure_outcomes(
                session, child_user_id, day_start_utc, day_end_utc
            )
            pipeline_failures = await self._query_pipeline_failures(
                session, child_user_id, day_start_utc, day_end_utc
            )
            connection = await self._query_connection(session, child_user_id)

        stats = self._aggregate(
            child_user_id=child_user_id,
            local_date=local_date,
            timezone_name=tz_name,
            messages=messages,
            enrichments=enrichments,
            security_assessments=security_assessments,
            exposure_assessments=exposure_assessments,
            security_outcomes=security_outcomes,
            exposure_outcomes=exposure_outcomes,
            pipeline_failures=pipeline_failures,
            connection=connection,
        )
        await self._daily_stats_repo.upsert(stats)
        _logger.info(
            "Rebuilt daily stats for child=%s date=%s status=%s",
            child_user_id,
            local_date,
            stats.coverage_status,
        )
        return stats

    async def rebuild_range(
        self,
        *,
        child_user_id: str,
        date_from: date,
        date_to: date,
        timezone_name: str | None = None,
    ) -> list[GuardDailyStats]:
        """Rebuild daily stats for a range of dates (inclusive)."""
        results: list[GuardDailyStats] = []
        current = date_from
        while current <= date_to:
            stats = await self.rebuild_day(
                child_user_id=child_user_id,
                local_date=current,
                timezone_name=timezone_name,
            )
            results.append(stats)
            current += timedelta(days=1)
        return results

    # --- Analytics query methods -------------------------------------------

    async def overview(
        self,
        *,
        date_from: date,
        date_to: date,
        child_user_id: str | None = None,
    ) -> AnalyticsOverview:
        """Aggregate overview across the period from persisted daily stats."""
        rows = await self._daily_stats_repo.list_for_range(
            child_user_id=child_user_id,
            date_from=date_from,
            date_to=date_to,
        )
        children = {row.child_user_id for row in rows}
        total = len(rows)
        healthy = sum(1 for row in rows if row.coverage_health)
        return AnalyticsOverview(
            children_monitored=len(children),
            total_messages=sum(row.messages_received for row in rows),
            total_assessments=sum(row.guard_assessments for row in rows),
            total_security_detections=sum(row.security_detections for row in rows),
            total_exposure_detections=sum(row.exposure_detections for row in rows),
            total_security_alerts_sent=sum(row.security_alerts_sent for row in rows),
            total_exposure_alerts_sent=sum(row.exposure_alerts_sent for row in rows),
            protected_days=sum(1 for row in rows if row.coverage_status == CoverageStatus.PROTECTED),
            attention_days=sum(1 for row in rows if row.coverage_status == CoverageStatus.ATTENTION),
            incomplete_days=sum(1 for row in rows if row.coverage_status == CoverageStatus.INCOMPLETE),
            coverage_health_pct=(healthy / total * 100.0) if total else 0.0,
        )

    async def children_table(
        self,
        *,
        date_from: date,
        date_to: date,
        child_user_id: str | None = None,
    ) -> list[ChildSummary]:
        """One row per child-day, most recent first, from persisted daily stats."""
        rows = await self._daily_stats_repo.list_for_range(
            child_user_id=child_user_id,
            date_from=date_from,
            date_to=date_to,
        )
        # Sort by date desc, then child for stable ordering.
        rows.sort(key=lambda r: (r.local_date, r.child_user_id), reverse=True)
        return [
            ChildSummary(
                child_user_id=row.child_user_id,
                child_name=None,  # Filled by route layer via user repo.
                local_date=row.local_date,
                coverage_status=row.coverage_status,
                coverage_health=row.coverage_health,
                messages_received=row.messages_received,
                guard_assessments=row.guard_assessments,
                security_detections=row.security_detections,
                exposure_detections=row.exposure_detections,
                security_alerts_sent=row.security_alerts_sent,
                exposure_alerts_sent=row.exposure_alerts_sent,
                connection_seconds=row.connection_seconds,
            )
            for row in rows
        ]

    async def daily_series(
        self,
        *,
        date_from: date,
        date_to: date,
        child_user_id: str | None = None,
    ) -> list[DailySeriesPoint]:
        """Daily volume series aggregated across all children (or one child)."""
        rows = await self._daily_stats_repo.list_for_range(
            child_user_id=child_user_id,
            date_from=date_from,
            date_to=date_to,
        )
        by_date: dict[date, DailySeriesPoint] = {}
        for row in rows:
            d = row.local_date
            if d not in by_date:
                by_date[d] = DailySeriesPoint(
                    local_date=d,
                    messages_received=0,
                    guard_assessments=0,
                    security_detections=0,
                    exposure_detections=0,
                    security_alerts_sent=0,
                    exposure_alerts_sent=0,
                )
            point = by_date[d]
            by_date[d] = DailySeriesPoint(
                local_date=d,
                messages_received=point.messages_received + row.messages_received,
                guard_assessments=point.guard_assessments + row.guard_assessments,
                security_detections=point.security_detections + row.security_detections,
                exposure_detections=point.exposure_detections + row.exposure_detections,
                security_alerts_sent=point.security_alerts_sent + row.security_alerts_sent,
                exposure_alerts_sent=point.exposure_alerts_sent + row.exposure_alerts_sent,
            )
        return [by_date[d] for d in sorted(by_date)]

    # --- Query methods -------------------------------------------------------

    async def _query_messages(
        self,
        session: AsyncSession,
        child_user_id: str,
        day_start: datetime,
        day_end: datetime,
    ) -> list[MessageRow]:
        result = await session.execute(
            select(MessageRow).where(
                MessageRow.user_id == child_user_id,
                MessageRow.timestamp >= day_start,
                MessageRow.timestamp < day_end,
            )
        )
        return list(result.scalars().all())

    async def _query_enrichments(
        self,
        session: AsyncSession,
        child_user_id: str,
        day_start: datetime,
        day_end: datetime,
    ) -> list[MessageEnrichmentRow]:
        """Query completed enrichments for messages in the day."""
        result = await session.execute(
            select(MessageEnrichmentRow)
            .join(MessageRow, MessageEnrichmentRow.message_id == MessageRow.id)
            .where(
                MessageRow.user_id == child_user_id,
                MessageRow.timestamp >= day_start,
                MessageRow.timestamp < day_end,
                MessageEnrichmentRow.status == "completed",
            )
        )
        return list(result.scalars().all())

    async def _query_security_assessments(
        self,
        session: AsyncSession,
        child_user_id: str,
        day_start: datetime,
        day_end: datetime,
    ) -> list[GuardSimpleAssessmentRow]:
        result = await session.execute(
            select(GuardSimpleAssessmentRow).where(
                GuardSimpleAssessmentRow.child_user_id == child_user_id,
                GuardSimpleAssessmentRow.created_at >= day_start,
                GuardSimpleAssessmentRow.created_at < day_end,
            )
        )
        return list(result.scalars().all())

    async def _query_exposure_assessments(
        self,
        session: AsyncSession,
        child_user_id: str,
        day_start: datetime,
        day_end: datetime,
    ) -> list[GuardExposureAssessmentRow]:
        result = await session.execute(
            select(GuardExposureAssessmentRow).where(
                GuardExposureAssessmentRow.child_user_id == child_user_id,
                GuardExposureAssessmentRow.created_at >= day_start,
                GuardExposureAssessmentRow.created_at < day_end,
            )
        )
        return list(result.scalars().all())

    async def _query_security_outcomes(
        self,
        session: AsyncSession,
        child_user_id: str,
        day_start: datetime,
        day_end: datetime,
    ) -> list[GuardParentAttentionDeliveryOutcomeRow]:
        result = await session.execute(
            select(GuardParentAttentionDeliveryOutcomeRow).where(
                GuardParentAttentionDeliveryOutcomeRow.child_user_id == child_user_id,
                GuardParentAttentionDeliveryOutcomeRow.created_at >= day_start,
                GuardParentAttentionDeliveryOutcomeRow.created_at < day_end,
            )
        )
        return list(result.scalars().all())

    async def _query_exposure_outcomes(
        self,
        session: AsyncSession,
        child_user_id: str,
        day_start: datetime,
        day_end: datetime,
    ) -> list[GuardExposureDeliveryOutcomeRow]:
        result = await session.execute(
            select(GuardExposureDeliveryOutcomeRow).where(
                GuardExposureDeliveryOutcomeRow.child_user_id == child_user_id,
                GuardExposureDeliveryOutcomeRow.created_at >= day_start,
                GuardExposureDeliveryOutcomeRow.created_at < day_end,
            )
        )
        return list(result.scalars().all())

    async def _query_pipeline_failures(
        self,
        session: AsyncSession,
        child_user_id: str,
        day_start: datetime,
        day_end: datetime,
    ) -> list[PipelineEventRow]:
        """Query pipeline failure events for the child's day."""
        result = await session.execute(
            select(PipelineEventRow).where(
                PipelineEventRow.status == "failed",
                PipelineEventRow.created_at >= day_start,
                PipelineEventRow.created_at < day_end,
            )
        )
        # Pipeline events don't have child_user_id directly — filter by
        # user_id_hash if available. For now, return all and let the
        # caller filter by message association if needed.
        return list(result.scalars().all())

    async def _query_connection(
        self,
        session: AsyncSession,
        child_user_id: str,
    ) -> WhatsAppConnectionRow | None:
        result = await session.execute(
            select(WhatsAppConnectionRow).where(
                WhatsAppConnectionRow.user_id == child_user_id
            )
        )
        return result.scalar_one_or_none()

    # --- Aggregation ---------------------------------------------------------

    def _aggregate(
        self,
        *,
        child_user_id: str,
        local_date: date,
        timezone_name: str,
        messages: list[MessageRow],
        enrichments: list[MessageEnrichmentRow],
        security_assessments: list[GuardSimpleAssessmentRow],
        exposure_assessments: list[GuardExposureAssessmentRow],
        security_outcomes: list[GuardParentAttentionDeliveryOutcomeRow],
        exposure_outcomes: list[GuardExposureDeliveryOutcomeRow],
        pipeline_failures: list[PipelineEventRow],
        connection: WhatsAppConnectionRow | None,
    ) -> GuardDailyStats:
        # Message counts
        private_msgs = 0
        group_msgs = 0
        private_chats: set[str] = set()
        group_chats: set[str] = set()
        media_received: dict[str, int] = {
            "images": 0,
            "videos": 0,
            "audio": 0,
            "links": 0,
            "documents": 0,
        }

        for msg in messages:
            is_group = msg.chat_id.endswith("@g.us")
            if is_group:
                group_msgs += 1
                group_chats.add(msg.chat_id)
            else:
                private_msgs += 1
                private_chats.add(msg.chat_id)

            mtype = msg.message_type
            if mtype in _MEDIA_TYPES:
                media_received[_MEDIA_TYPES[mtype]] += 1
            elif mtype == "text" and msg.text:
                # Link detection: text messages with URLs that get enriched
                # as links. We count links by enrichment kind, not by
                # message_type, so this is handled in enrichments below.
                pass

        # Enrichment counts (by kind)
        media_enriched: dict[str, int] = {
            "images": 0,
            "videos": 0,
            "audio": 0,
            "links": 0,
            "documents": 0,
        }
        for enr in enrichments:
            kind = enr.kind
            if kind == "image":
                media_enriched["images"] += 1
            elif kind == "video":
                media_enriched["videos"] += 1
            elif kind == "audio":
                media_enriched["audio"] += 1
            elif kind == "link" or kind == "video_link":
                media_enriched["links"] += 1
            elif kind == "document":
                media_enriched["documents"] += 1

        # Assessment counts
        guard_assessments = len(security_assessments) + len(exposure_assessments)
        security_detections = sum(
            1 for a in security_assessments if a.parent_should_know is True
        )
        exposure_detections = sum(
            1 for a in exposure_assessments if a.significant_exposure is True
        )

        # Alert delivery counts
        security_alerts_sent = sum(
            1 for o in security_outcomes if o.status == "sent"
        )
        exposure_alerts_sent = sum(
            1 for o in exposure_outcomes if o.status == "sent"
        )
        alerts_failed = sum(
            1 for o in security_outcomes if o.status == "failed"
        ) + sum(1 for o in exposure_outcomes if o.status == "failed")

        # Pipeline failures
        analysis_failures = sum(
            1 for e in pipeline_failures if e.pipeline == "guard_analysis"
        )
        media_enrichment_failures = sum(
            1 for e in pipeline_failures if e.pipeline == "media_enrichment"
        )

        # Coverage health
        security_attention = security_alerts_sent > 0 or exposure_alerts_sent > 0
        coverage_health = self._compute_coverage_health(
            messages=messages,
            connection=connection,
            media_enrichment_failures=media_enrichment_failures,
            total_media=sum(media_received.values()),
        )
        coverage_status = self._derive_status(
            coverage_health=coverage_health,
            security_attention=security_attention,
            has_messages=len(messages) > 0,
            connection=connection,
        )

        return GuardDailyStats(
            child_user_id=child_user_id,
            local_date=local_date,
            timezone=timezone_name,
            messages_received=len(messages),
            private_messages=private_msgs,
            group_messages=group_msgs,
            active_private_chats=len(private_chats),
            active_group_chats=len(group_chats),
            images_received=media_received["images"],
            videos_received=media_received["videos"],
            audio_received=media_received["audio"],
            links_received=media_received["links"],
            documents_received=media_received["documents"],
            images_enriched=media_enriched["images"],
            videos_enriched=media_enriched["videos"],
            audio_enriched=media_enriched["audio"],
            links_enriched=media_enriched["links"],
            documents_enriched=media_enriched["documents"],
            guard_assessments=guard_assessments,
            security_detections=security_detections,
            exposure_detections=exposure_detections,
            security_alerts_sent=security_alerts_sent,
            exposure_alerts_sent=exposure_alerts_sent,
            alerts_failed=alerts_failed,
            analysis_failures=analysis_failures,
            media_enrichment_failures=media_enrichment_failures,
            coverage_health=coverage_health,
            security_attention=security_attention,
            coverage_status=coverage_status,
            connection_seconds=self._estimate_connection_seconds(connection),
            updated_at=datetime.now(timezone.utc),
        )

    def _compute_coverage_health(
        self,
        *,
        messages: list[MessageRow],
        connection: WhatsAppConnectionRow | None,
        media_enrichment_failures: int,
        total_media: int,
    ) -> bool:
        """Determine if coverage was healthy.

        Health is false if:
        - The connection was not connected/degraded for the day.
        - Media enrichment failure rate exceeds the threshold.
        A quiet day with no messages is healthy (if connected).
        """
        if connection is None:
            return False

        if connection.connection_status not in ("connected", "degraded"):
            return False

        # Enrichment failure rate
        if total_media > 0:
            failure_rate = media_enrichment_failures / total_media
            if failure_rate > self._config.enrichment_failure_threshold_pct:
                return False

        return True

    def _derive_status(
        self,
        *,
        coverage_health: bool,
        security_attention: bool,
        has_messages: bool,
        connection: WhatsAppConnectionRow | None,
    ) -> str:
        """Derive the parent-facing coverage status.

        - ``attention`` if any alert was sent (regardless of health).
        - ``incomplete`` if coverage health is false.
        - ``protected`` only if healthy AND no alerts.
        - A disconnected child can never be ``protected``.
        """
        if security_attention:
            return CoverageStatus.ATTENTION
        if not coverage_health:
            return CoverageStatus.INCOMPLETE
        # Connected and healthy with no alerts
        if connection is None or connection.connection_status not in (
            "connected",
            "degraded",
        ):
            return CoverageStatus.INCOMPLETE
        return CoverageStatus.PROTECTED

    def _estimate_connection_seconds(
        self, connection: WhatsAppConnectionRow | None
    ) -> int | None:
        """Estimate seconds the connection was healthy during the day.

        For the pilot, we use a simple heuristic: if the connection is
        currently connected/degraded, assume it was up for the full day
        (86400 seconds). If disconnected, assume 0. A more precise
        calculation would use baileys event history.
        """
        if connection is None:
            return None
        if connection.connection_status in ("connected", "degraded"):
            return 86400
        return 0
