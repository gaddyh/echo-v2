"""Parent-attention delivery state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ParentAttentionDeliveryState:
    child_user_id: str
    chat_id: str
    last_delivered_assessment_id: str | None = None
    last_delivered_evidence_ids: tuple[str, ...] = ()
    last_delivered_at: datetime | None = None
    last_action_id: str | None = None
    resolved_at: datetime | None = None


__all__ = ["ParentAttentionDeliveryState"]
