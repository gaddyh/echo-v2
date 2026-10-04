"""Domain contracts for the Guard shadow analysis pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from echo_v2.services.guard_taxonomy import GuardDecision

__all__ = [
    "GuardAnalysisRecord",
    "GuardChatState",
    "GuardianChildLink",
    "GuardianChildLinkStatus",
]


class GuardianChildLinkStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


@dataclass(frozen=True)
class GuardianChildLink:
    id: str
    guardian_user_id: str
    child_user_id: str
    status: GuardianChildLinkStatus
    child_consented_at: datetime | None = None
    safety_enabled_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_guard_enabled(self) -> bool:
        return (
            self.status == GuardianChildLinkStatus.ACTIVE
            and self.safety_enabled_at is not None
        )


@dataclass
class GuardChatState:
    child_user_id: str
    chat_id: str
    activity_version: int
    last_message_at: datetime
    last_analyzed_version: int = 0
    last_decision: GuardDecision = GuardDecision.NONE
    last_analysis_at: datetime | None = None
    pending_since: datetime | None = None
    next_analysis_at: datetime | None = None
    chat_name: str | None = None
    is_group: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class GuardAnalysisRecord:
    child_user_id: str
    connection_id: str
    chat_id: str
    target_version: int
    signals: tuple[str, ...]
    categories: tuple[str, ...]
    confidence: float
    reason: str
    evidence_message_ids: tuple[str, ...]
    decision: GuardDecision
    model: str
    prompt_version: str
    analyzer_version: str
    taxonomy_version: str
    created_at: datetime | None = None
    id: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
