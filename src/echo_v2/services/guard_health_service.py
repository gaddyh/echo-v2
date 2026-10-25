"""GuardHealthService — system health snapshot for the admin dashboard.

Queries live data from source tables to surface operational health:
connections, message flow, assessment activity, enrichment status,
AI costs, alerts, and stuck work. No LLM calls — all data is read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.persistence.orm import (
    AiUsageEventRow,
    BaileysConnectorEventProcessingRow,
    GuardAlertRow,
    GuardDailyStatsRow,
    GuardExposureAssessmentRow,
    GuardParentAttentionDeliveryOutcomeRow,
    GuardSimpleAssessmentRow,
    MessageEnrichmentRow,
    MessageRow,
    PipelineEventRow,
    WhatsAppConnectionRow,
)

__all__ = ["GuardHealthService", "HealthSnapshot"]


@dataclass(frozen=True)
class HealthSnapshot:
    """Complete system health snapshot."""

    generated_at: datetime
    connections: list[dict[str, Any]]
    connection_summary: dict[str, int]
    messages_last_1h: int
    messages_last_24h: int
    latest_message_at: datetime | None
    security_assessments_last_1h: int
    security_assessments_last_24h: int
    exposure_assessments_last_1h: int
    exposure_assessments_last_24h: int
    psk_none_last_24h: int
    psk_true_last_24h: int
    enrichment_status_24h: dict[str, int]
    terminal_errors_24h: list[dict[str, Any]]
    stuck_enrichments: list[dict[str, Any]]
    failed_enrichments_24h: int
    pipeline_events_1h: dict[str, int]
    ai_usage_events_1h: int
    ai_usage_cost_1h: float
    ai_usage_events_24h: int
    ai_usage_cost_24h: float
    guard_alerts_24h: int
    delivery_outcomes_24h: dict[str, int]
    daily_stats_rows: int
    daily_stats_last_updated: datetime | None
    connector_events_24h: dict[str, int]


class GuardHealthService:
    """Read-only health queries for the admin dashboard."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def snapshot(self) -> HealthSnapshot:
        now = datetime.now(timezone.utc)
        hour_ago = now - timedelta(hours=1)
        day_ago = now - timedelta(days=1)

        async with self._session_factory() as session:
            connections = await self._query_connections(session)
            connection_summary = self._summarize_connections(connections)
            messages_1h, latest_msg = await self._count_messages(
                session, hour_ago
            )
            messages_24h, _ = await self._count_messages(session, day_ago)
            sec_1h, sec_24h = await self._count_security(session, hour_ago, day_ago)
            exp_1h, exp_24h = await self._count_exposure(session, hour_ago, day_ago)
            psk_none, psk_true = await self._assessment_quality(session, day_ago)
            enrichment_status = await self._enrichment_status(session, day_ago)
            terminal_errors = await self._terminal_errors(session, day_ago)
            stuck = await self._stuck_enrichments(session, hour_ago)
            failed_24h = await self._failed_enrichments(session, day_ago)
            pipeline_1h = await self._pipeline_events(session, hour_ago)
            ai_1h, ai_cost_1h = await self._ai_usage(session, hour_ago)
            ai_24h, ai_cost_24h = await self._ai_usage(session, day_ago)
            alerts_24h = await self._guard_alerts(session, day_ago)
            delivery = await self._delivery_outcomes(session, day_ago)
            stats_rows, stats_updated = await self._daily_stats_info(session)
            connector = await self._connector_events(session, day_ago)

        return HealthSnapshot(
            generated_at=now,
            connections=connections,
            connection_summary=connection_summary,
            messages_last_1h=messages_1h,
            messages_last_24h=messages_24h,
            latest_message_at=latest_msg,
            security_assessments_last_1h=sec_1h,
            security_assessments_last_24h=sec_24h,
            exposure_assessments_last_1h=exp_1h,
            exposure_assessments_last_24h=exp_24h,
            psk_none_last_24h=psk_none,
            psk_true_last_24h=psk_true,
            enrichment_status_24h=enrichment_status,
            terminal_errors_24h=terminal_errors,
            stuck_enrichments=stuck,
            failed_enrichments_24h=failed_24h,
            pipeline_events_1h=pipeline_1h,
            ai_usage_events_1h=ai_1h,
            ai_usage_cost_1h=ai_cost_1h,
            ai_usage_events_24h=ai_24h,
            ai_usage_cost_24h=ai_cost_24h,
            guard_alerts_24h=alerts_24h,
            delivery_outcomes_24h=delivery,
            daily_stats_rows=stats_rows,
            daily_stats_last_updated=stats_updated,
            connector_events_24h=connector,
        )

    async def _query_connections(
        self, session: AsyncSession
    ) -> list[dict[str, Any]]:
        result = await session.execute(
            select(
                WhatsAppConnectionRow.user_id,
                WhatsAppConnectionRow.connection_status,
                WhatsAppConnectionRow.provider,
                WhatsAppConnectionRow.updated_at,
                WhatsAppConnectionRow.baileys_state_version,
            ).order_by(WhatsAppConnectionRow.updated_at.desc())
        )
        return [
            {
                "user_id": str(row.user_id),
                "status": row.connection_status,
                "provider": row.provider,
                "updated_at": row.updated_at,
                "state_version": row.baileys_state_version,
            }
            for row in result
        ]

    def _summarize_connections(
        self, connections: list[dict[str, Any]]
    ) -> dict[str, int]:
        summary: dict[str, int] = {}
        for c in connections:
            summary[c["status"]] = summary.get(c["status"], 0) + 1
        return summary

    async def _count_messages(
        self, session: AsyncSession, since: datetime
    ) -> tuple[int, datetime | None]:
        result = await session.execute(
            select(func.count(MessageRow.id), func.max(MessageRow.timestamp)).where(
                MessageRow.timestamp >= since
            )
        )
        row = result.one()
        return row[0] or 0, row[1]

    async def _count_security(
        self, session: AsyncSession, hour_ago: datetime, day_ago: datetime
    ) -> tuple[int, int]:
        r1 = await session.execute(
            select(func.count(GuardSimpleAssessmentRow.id)).where(
                GuardSimpleAssessmentRow.created_at >= hour_ago
            )
        )
        r2 = await session.execute(
            select(func.count(GuardSimpleAssessmentRow.id)).where(
                GuardSimpleAssessmentRow.created_at >= day_ago
            )
        )
        return r1.scalar() or 0, r2.scalar() or 0

    async def _count_exposure(
        self, session: AsyncSession, hour_ago: datetime, day_ago: datetime
    ) -> tuple[int, int]:
        r1 = await session.execute(
            select(func.count(GuardExposureAssessmentRow.id)).where(
                GuardExposureAssessmentRow.created_at >= hour_ago
            )
        )
        r2 = await session.execute(
            select(func.count(GuardExposureAssessmentRow.id)).where(
                GuardExposureAssessmentRow.created_at >= day_ago
            )
        )
        return r1.scalar() or 0, r2.scalar() or 0

    async def _assessment_quality(
        self, session: AsyncSession, day_ago: datetime
    ) -> tuple[int, int]:
        result = await session.execute(
            select(GuardSimpleAssessmentRow.parent_should_know).where(
                GuardSimpleAssessmentRow.created_at >= day_ago
            )
        )
        psk_none = 0
        psk_true = 0
        for (psk,) in result:
            if psk is None:
                psk_none += 1
            elif psk is True:
                psk_true += 1
        return psk_none, psk_true

    async def _enrichment_status(
        self, session: AsyncSession, day_ago: datetime
    ) -> dict[str, int]:
        result = await session.execute(
            select(
                MessageEnrichmentRow.status,
                func.count(MessageEnrichmentRow.id),
            )
            .where(MessageEnrichmentRow.created_at >= day_ago)
            .group_by(MessageEnrichmentRow.status)
        )
        return {row[0]: row[1] for row in result}

    async def _terminal_errors(
        self, session: AsyncSession, day_ago: datetime
    ) -> list[dict[str, Any]]:
        result = await session.execute(
            select(
                MessageEnrichmentRow.error,
                func.count(MessageEnrichmentRow.id),
            )
            .where(
                MessageEnrichmentRow.status == "terminal",
                MessageEnrichmentRow.created_at >= day_ago,
            )
            .group_by(MessageEnrichmentRow.error)
            .order_by(func.count(MessageEnrichmentRow.id).desc())
            .limit(10)
        )
        return [
            {"error": (row[0] or "(none)")[:200], "count": row[1]}
            for row in result
        ]

    async def _stuck_enrichments(
        self, session: AsyncSession, hour_ago: datetime
    ) -> list[dict[str, Any]]:
        result = await session.execute(
            select(
                MessageEnrichmentRow.id,
                MessageEnrichmentRow.message_id,
                MessageEnrichmentRow.created_at,
                MessageEnrichmentRow.updated_at,
                MessageEnrichmentRow.lease_expires_at,
            ).where(
                MessageEnrichmentRow.status == "processing",
                MessageEnrichmentRow.created_at < hour_ago,
            )
        )
        return [
            {
                "id": str(row.id),
                "message_id": str(row.message_id),
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "lease_expires_at": row.lease_expires_at,
            }
            for row in result
        ]

    async def _failed_enrichments(
        self, session: AsyncSession, day_ago: datetime
    ) -> int:
        result = await session.execute(
            select(func.count(MessageEnrichmentRow.id)).where(
                MessageEnrichmentRow.status == "failed",
                MessageEnrichmentRow.created_at >= day_ago,
            )
        )
        return result.scalar() or 0

    async def _pipeline_events(
        self, session: AsyncSession, hour_ago: datetime
    ) -> dict[str, int]:
        result = await session.execute(
            select(
                PipelineEventRow.event_name,
                func.count(PipelineEventRow.id),
            )
            .where(PipelineEventRow.created_at >= hour_ago)
            .group_by(PipelineEventRow.event_name)
        )
        return {row[0]: row[1] for row in result}

    async def _ai_usage(
        self, session: AsyncSession, since: datetime
    ) -> tuple[int, float]:
        result = await session.execute(
            select(
                func.count(AiUsageEventRow.id),
                func.coalesce(func.sum(AiUsageEventRow.estimated_cost_usd), 0),
            ).where(AiUsageEventRow.created_at >= since)
        )
        row = result.one()
        return row[0] or 0, float(row[1] or 0)

    async def _guard_alerts(
        self, session: AsyncSession, day_ago: datetime
    ) -> int:
        result = await session.execute(
            select(func.count(GuardAlertRow.id)).where(
                GuardAlertRow.created_at >= day_ago
            )
        )
        return result.scalar() or 0

    async def _delivery_outcomes(
        self, session: AsyncSession, day_ago: datetime
    ) -> dict[str, int]:
        result = await session.execute(
            select(
                GuardParentAttentionDeliveryOutcomeRow.status,
                func.count(GuardParentAttentionDeliveryOutcomeRow.assessment_id),
            )
            .where(GuardParentAttentionDeliveryOutcomeRow.created_at >= day_ago)
            .group_by(GuardParentAttentionDeliveryOutcomeRow.status)
        )
        return {row[0]: row[1] for row in result}

    async def _daily_stats_info(
        self, session: AsyncSession
    ) -> tuple[int, datetime | None]:
        result = await session.execute(
            select(
                func.count(GuardDailyStatsRow.child_user_id),
                func.max(GuardDailyStatsRow.updated_at),
            )
        )
        row = result.one()
        return row[0] or 0, row[1]

    async def _connector_events(
        self, session: AsyncSession, day_ago: datetime
    ) -> dict[str, int]:
        result = await session.execute(
            select(
                BaileysConnectorEventProcessingRow.status,
                func.count(BaileysConnectorEventProcessingRow.event_id),
            )
            .where(BaileysConnectorEventProcessingRow.updated_at >= day_ago)
            .group_by(BaileysConnectorEventProcessingRow.status)
        )
        return {row[0]: row[1] for row in result}
