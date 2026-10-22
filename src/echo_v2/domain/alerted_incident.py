"""Alerted-incident domain model for the Incident Tracker.

An ``AlertedIncident`` exists because a parent alert was durably scheduled.
It serves as semantic deduplication memory: subsequent YES assessments are
matched against alerted incidents to decide whether to suppress, escalate,
or create a new alert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

__all__ = ["AlertedIncident"]


@dataclass(frozen=True)
class AlertedIncident:
    """A previously-alerted incident used for semantic deduplication.

    The incident row is created when its initial notification is durably
    scheduled (not when WhatsApp confirms delivery), preventing the next
    YES assessment from triggering a duplicate.
    """

    id: str
    child_user_id: str
    chat_id: str
    alert_type: str  # "security" | "exposure"
    summary: str  # latest issue summary
    severity_summary: str  # highest severity observed
    notified_severity_summary: str | None  # highest severity already notified
    evidence_message_ids: tuple[str, ...]
    assessment_ids: tuple[str, ...]
    last_seen_at: datetime
    closed_at: datetime | None
    related_incident_id: str | None  # recurrence link to closed incident
    notification_version: int  # 1=initial, 2=first escalation, etc.
    created_at: datetime
    updated_at: datetime
