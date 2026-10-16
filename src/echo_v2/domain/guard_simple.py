"""Domain contracts for the independent Baseline B Guard analyzer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SafetyStatus(str, Enum):
    SAFE = "safe"
    ATTENTION = "attention"
    URGENT = "urgent"
    UNCERTAIN = "uncertain"


class ChildRole(str, Enum):
    TARGET = "target"
    PARTICIPANT = "participant"
    BYSTANDER = "bystander"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SimpleConversationMessage:
    id: str
    sender: str
    text: str


@dataclass(frozen=True)
class PriorConcern:
    status: SafetyStatus
    child_role: ChildRole
    summary: str
    reason: str
    evidence_message_ids: tuple[str, ...]
    created_at: datetime
    assessment_id: str | None = None


@dataclass(frozen=True)
class SimpleConversation:
    child_user_id: str
    chat_id: str
    chat_type: str
    connection_id: str
    timezone: str
    window_start: datetime
    window_end: datetime
    monitored_child_role: str
    messages: tuple[SimpleConversationMessage, ...] = ()
    truncated: bool = False
    omitted_message_count: int = 0
    identity_verified: bool = True
    identity_reason: str | None = None
    prior_concern: PriorConcern | None = None

    @property
    def input_message_ids(self) -> tuple[str, ...]:
        return tuple(message.id for message in self.messages)


@dataclass(frozen=True)
class SafetyAssessment:
    """Immutable semantic assessment plus provenance for assessment history."""

    status: SafetyStatus
    child_role: ChildRole
    summary: str
    evidence_message_ids: tuple[str, ...]
    reason: str
    child_user_id: str = ""
    connection_id: str = ""
    chat_id: str = ""
    chat_type: str = ""
    timezone: str = ""
    window_start: datetime | None = None
    window_end: datetime | None = None
    truncated: bool = False
    omitted_message_count: int = 0
    identity_verified: bool = True
    identity_reason: str | None = None
    model: str = ""
    prompt_version: str = ""
    analyzer_version: str = ""
    created_at: datetime | None = None
    id: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "ChildRole",
    "PriorConcern",
    "SafetyAssessment",
    "SafetyStatus",
    "SimpleConversation",
    "SimpleConversationMessage",
]
