"""Parent-attention delivery state and per-assessment outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


@dataclass(frozen=True)
class ParentAttentionDeliveryState:
    guardian_user_id: str
    child_user_id: str
    chat_id: str
    last_delivered_assessment_id: str | None = None
    last_delivered_evidence_ids: tuple[str, ...] = ()
    last_scheduled_at: datetime | None = None
    last_action_id: str | None = None
    resolved_at: datetime | None = None


class ParentAttentionOutcomeStatus(str, Enum):
    NOT_SENT = "not_sent"
    SCHEDULED = "scheduled"
    SENT = "sent"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class ParentAttentionDeliveryOutcome:
    assessment_id: str
    child_user_id: str
    chat_id: str
    status: ParentAttentionOutcomeStatus
    reason: str
    guardian_user_id: str = ""
    scheduled_action_id: str | None = None
    provider_message_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    scheduled_at: datetime | None = None
    sent_at: datetime | None = None
    failed_at: datetime | None = None


__all__ = [
    "ParentAttentionDeliveryOutcome",
    "ParentAttentionDeliveryState",
    "ParentAttentionOutcomeStatus",
]
