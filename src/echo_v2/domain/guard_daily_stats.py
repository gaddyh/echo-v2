"""Domain model for daily Guard reporting statistics.

One ``GuardDailyStats`` row represents the aggregated activity, detections,
alerts, and coverage health for a single child on a single local calendar day.

Source data comes from existing tables — no new LLM analysis is triggered:

- ``messages`` → message/media counts (by ``timestamp`` in the child's timezone)
- ``message_enrichments`` → enriched media counts
- ``guard_simple_assessments`` → guard_assessments, security_detections
- ``guard_exposure_assessments`` → exposure_detections
- delivery outcomes → security_alerts_sent, exposure_alerts_sent, alerts_failed
- ``pipeline_events`` → analysis_failures, media_enrichment_failures
- ``whatsapp_connections`` → connection_seconds, coverage_health

Detections (analyzer YES) and sent alerts (delivered) are separate counts.
A disconnected child can never be ``protected``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Final

__all__ = ["CoverageStatus", "GuardDailyStats"]


class CoverageStatus:
    """Parent-facing coverage status for a single child-day."""

    PROTECTED: Final[str] = "protected"
    """Connection + ingestion + analysis healthy, no alert-worthy activity."""

    ATTENTION: Final[str] = "attention"
    """At least one security or exposure alert was sent."""

    INCOMPLETE: Final[str] = "incomplete"
    """Coverage health is false — disconnected, delayed, or unprocessed."""


@dataclass(frozen=True)
class GuardDailyStats:
    """Aggregated daily statistics for one child on one local date.

    Attributes:
        child_user_id: UUID of the monitored child.
        local_date: Calendar day in the child's timezone (by message timestamp).
        timezone: Timezone name used for day boundary computation.
        messages_received: Total deduplicated messages received that day.
        private_messages: Messages in 1:1 chats.
        group_messages: Messages in group chats.
        active_private_chats: Distinct 1:1 chats with activity that day.
        active_group_chats: Distinct group chats with activity that day.
        images_received: Image messages received.
        videos_received: Video messages received.
        audio_received: Audio messages received.
        links_received: Link messages received.
        documents_received: Document messages received.
        images_enriched: Images successfully enriched (status=completed).
        videos_enriched: Videos successfully enriched.
        audio_enriched: Audio successfully enriched.
        links_enriched: Links successfully enriched.
        documents_enriched: Documents successfully enriched.
        guard_assessments: Total Guard assessments (security + exposure).
        security_detections: Security assessments with parent_should_know=True.
        exposure_detections: Exposure assessments with significant_exposure=True.
        security_alerts_sent: Security alerts actually delivered (not just detected).
        exposure_alerts_sent: Exposure alerts actually delivered.
        alerts_failed: Delivery outcomes with failed status.
        analysis_failures: Pipeline analysis failures that day.
        media_enrichment_failures: Media enrichment failures that day.
        coverage_health: True if connection + processing + enrichment are healthy.
        security_attention: True if any alert was sent that day.
        coverage_status: Derived parent-facing status (protected/attention/incomplete).
        connection_seconds: Estimated seconds the connection was healthy.
        updated_at: When this row was last rebuilt.
    """

    child_user_id: str
    local_date: date
    timezone: str
    messages_received: int = 0
    private_messages: int = 0
    group_messages: int = 0
    active_private_chats: int = 0
    active_group_chats: int = 0
    images_received: int = 0
    videos_received: int = 0
    audio_received: int = 0
    links_received: int = 0
    documents_received: int = 0
    images_enriched: int = 0
    videos_enriched: int = 0
    audio_enriched: int = 0
    links_enriched: int = 0
    documents_enriched: int = 0
    guard_assessments: int = 0
    security_detections: int = 0
    exposure_detections: int = 0
    security_alerts_sent: int = 0
    exposure_alerts_sent: int = 0
    alerts_failed: int = 0
    analysis_failures: int = 0
    media_enrichment_failures: int = 0
    coverage_health: bool = True
    security_attention: bool = False
    coverage_status: str = CoverageStatus.PROTECTED
    connection_seconds: int | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
