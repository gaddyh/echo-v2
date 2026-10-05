"""Operator feedback types for Guard shadow review."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class GuardFeedbackLabel(str, Enum):
    CORRECT = "correct"
    FALSE_POSITIVE = "false_positive"
    SHOULD_BE_STRONGER = "should_be_stronger"
    IRRELEVANT = "irrelevant"


@dataclass(frozen=True)
class GuardAnalysisFeedback:
    result_id: str
    reviewer_user_id: str
    label: GuardFeedbackLabel
    note: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class GuardFeedbackEvent:
    id: str
    result_id: str
    reviewer_user_id: str
    old_label: GuardFeedbackLabel | None
    new_label: GuardFeedbackLabel
    old_note: str | None
    new_note: str | None
    changed_at: datetime
