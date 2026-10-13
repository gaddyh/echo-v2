"""Domain contracts for isolated Guard Live Compare runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class GuardEvalRunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class GuardEvalJudgmentLabel(str, Enum):
    ACCEPTED_DIFFERENCE = "accepted_difference"
    REAL_FAILURE = "real_failure"
    GOLD_NEEDS_CORRECTION = "gold_needs_correction"
    UNDECIDED = "undecided"


@dataclass(frozen=True)
class GuardEvalAdmission:
    run: GuardEvalRun | None
    allowed: bool
    reason: str | None = None
    duplicate: bool = False


@dataclass(frozen=True)
class GuardEvalRun:
    id: str
    scenario_id: str
    requester_key: str
    request_id: str
    gold_version: str
    gold_label: str
    status: GuardEvalRunStatus
    model: str
    prompt_version: str
    analyzer_version: str
    taxonomy_version: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class GuardEvalSnapshot:
    id: str
    run_id: str
    scenario_id: str
    ordinal: int
    after_message_id: str
    expected: dict[str, Any]
    actual: dict[str, Any]
    diff: dict[str, Any]
    has_difference: bool
    failure: bool
    created_at: datetime


@dataclass(frozen=True)
class GuardEvalJudgment:
    snapshot_id: str
    reviewer_key: str
    label: GuardEvalJudgmentLabel
    note: str | None
    reason: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class GuardEvalRunBundle:
    run: GuardEvalRun
    snapshots: tuple[GuardEvalSnapshot, ...]
    judgments: tuple[GuardEvalJudgment, ...]


__all__ = [
    "GuardEvalAdmission",
    "GuardEvalJudgment",
    "GuardEvalJudgmentLabel",
    "GuardEvalRun",
    "GuardEvalRunBundle",
    "GuardEvalRunStatus",
    "GuardEvalSnapshot",
]
