"""Domain contracts for Guard alert orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class GuardAlertKind(str, Enum):
    NOTIFY = "notify"
    URGENT = "urgent"


class GuardAlertStatus(str, Enum):
    SCHEDULED = "scheduled"
    SUPPRESSED = "suppressed"
    EXPIRED = "expired"


class GuardThresholdStateStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class GuardAlertEvaluationOutcome(str, Enum):
    SCHEDULED = "scheduled"
    NO_ACTION = "no_action"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class GuardThresholdState:
    child_user_id: str
    chat_id: str
    threshold_family: str
    status: GuardThresholdStateStatus = GuardThresholdStateStatus.INACTIVE
    episode_start_version: int | None = None
    current_kind: GuardAlertKind | None = None
    last_processed_version: int = 0
    last_supporting_version: int | None = None
    last_supporting_at: datetime | None = None
    non_actionable_count: int = 0
    non_actionable_since: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class GuardAlertEvaluation:
    analysis_result_id: str
    child_user_id: str
    chat_id: str
    target_version: int
    policy_version: str
    outcome: GuardAlertEvaluationOutcome
    evaluated_at: datetime
    alert_id: str | None = None


@dataclass(frozen=True)
class GuardAlert:
    id: str
    analysis_result_id: str
    guardian_user_id: str
    child_user_id: str
    chat_id: str
    kind: GuardAlertKind
    categories: tuple[str, ...]
    summary: str
    policy_reason: str
    policy_version: str
    status: GuardAlertStatus
    scheduled_action_id: str
    logical_event_key: str
    threshold_family: str
    episode_start_version: int
    detected_at: datetime
    expires_at: datetime
    created_at: datetime
    metadata: dict[str, Any] | None = None
