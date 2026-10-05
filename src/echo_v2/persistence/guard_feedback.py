"""Current Guard feedback and append-only audit persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from echo_v2.domain.guard_feedback import (
    GuardAnalysisFeedback,
    GuardFeedbackEvent,
    GuardFeedbackLabel,
)


class GuardFeedbackRepository(Protocol):
    async def set_feedback(
        self, *, result_id: str, reviewer_user_id: str, label: GuardFeedbackLabel, note: str | None
    ) -> GuardAnalysisFeedback | None: ...

    async def get_for_results(self, result_ids: list[str]) -> dict[str, GuardAnalysisFeedback]: ...

    async def list_events(self, result_id: str) -> list[GuardFeedbackEvent]: ...


class InMemoryGuardFeedbackRepository:
    def __init__(self) -> None:
        self.feedback: dict[str, GuardAnalysisFeedback] = {}
        self.events: list[GuardFeedbackEvent] = []

    async def set_feedback(
        self, *, result_id: str, reviewer_user_id: str, label: GuardFeedbackLabel, note: str | None
    ) -> GuardAnalysisFeedback:
        now = datetime.now(timezone.utc)
        old = self.feedback.get(result_id)
        self.events.append(
            GuardFeedbackEvent(
                id=f"feedback-event-{len(self.events) + 1}", result_id=result_id,
                reviewer_user_id=reviewer_user_id, old_label=old.label if old else None,
                new_label=label, old_note=old.note if old else None, new_note=note, changed_at=now,
            )
        )
        current = GuardAnalysisFeedback(
            result_id=result_id, reviewer_user_id=reviewer_user_id,
            label=label, note=note, created_at=old.created_at if old else now, updated_at=now,
        )
        self.feedback[result_id] = current
        return current

    async def get_for_results(self, result_ids: list[str]) -> dict[str, GuardAnalysisFeedback]:
        return {key: self.feedback[key] for key in result_ids if key in self.feedback}

    async def list_events(self, result_id: str) -> list[GuardFeedbackEvent]:
        return [event for event in self.events if event.result_id == result_id]
