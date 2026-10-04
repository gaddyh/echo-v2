"""Deterministic scheduling policy for Guard re-analysis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import ClassVar

from echo_v2.domain.guard import GuardChatState
from echo_v2.services.guard_taxonomy import GuardDecision

__all__ = [
    "GuardConversationState",
    "GuardSchedule",
    "GuardSchedulePolicy",
    "GuardSchedulingContext",
]


@dataclass(frozen=True)
class GuardConversationState:
    decision: GuardDecision = GuardDecision.NONE
    active_signals: tuple[str, ...] = ()
    active_categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class GuardSchedulingContext:
    state: GuardConversationState
    is_group: bool
    pending_since: datetime | None
    last_analysis_at: datetime | None
    next_analysis_at: datetime | None
    new_message_at: datetime


@dataclass(frozen=True)
class GuardSchedule:
    analyze_at: datetime
    reason: str


class GuardSchedulePolicy:
    """Choose when new activity should receive another Guard look."""

    _INTERVALS: ClassVar[dict[GuardDecision, timedelta]] = {
        GuardDecision.NONE: timedelta(seconds=120),
        GuardDecision.WATCH: timedelta(seconds=60),
        GuardDecision.CONCERNING: timedelta(seconds=30),
        GuardDecision.URGENT: timedelta(seconds=0),
    }

    def on_message(self, context: GuardSchedulingContext) -> GuardSchedule:
        candidate = context.new_message_at + self._INTERVALS[context.state.decision]
        if context.next_analysis_at is not None:
            analyze_at = min(context.next_analysis_at, candidate)
            reason = "existing_deadline_or_decision_deadline"
        else:
            analyze_at = candidate
            reason = f"{context.state.decision.value}_with_new_activity"
        return GuardSchedule(analyze_at=analyze_at, reason=reason)

    def after_analysis(
        self,
        *,
        state: GuardChatState,
        analyzed_at: datetime | None = None,
    ) -> GuardSchedule | None:
        """Clear the pending batch once analysis caught up to its version."""
        del state
        del analyzed_at
        return None

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
