"""Domain contracts for Guard operations telemetry and dashboard data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class GuardAnalysisAttemptStatus(str, Enum):
    STARTED = "started"
    COMMITTED = "committed"
    STALE = "stale"
    FAILED = "failed"


@dataclass(frozen=True)
class GuardAnalysisAttempt:
    id: str
    child_user_id: str
    chat_id: str
    target_version: int
    status: GuardAnalysisAttemptStatus
    started_at: datetime
    scheduled_for: datetime | None = None
    pending_since: datetime | None = None
    finished_at: datetime | None = None
    result_id: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    analyzer_version: str | None = None
    error_code: str | None = None


__all__ = ["GuardAnalysisAttempt", "GuardAnalysisAttemptStatus"]
