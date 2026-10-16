"""Persistence contracts for parent-attention delivery state and outcomes."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol

from echo_v2.domain.parent_attention import (
    ParentAttentionDeliveryOutcome,
    ParentAttentionDeliveryState,
)


class ParentAttentionDeliveryRepository(Protocol):
    async def get(self, *, child_user_id: str, chat_id: str) -> ParentAttentionDeliveryState | None: ...

    async def save(self, state: ParentAttentionDeliveryState) -> None: ...

    async def resolve(self, *, child_user_id: str, chat_id: str, resolved_at: datetime | None = None) -> None: ...


class ParentAttentionDeliveryOutcomeRepository(Protocol):
    async def get(self, assessment_id: str) -> ParentAttentionDeliveryOutcome | None: ...

    async def list_for_assessments(
        self, assessment_ids: list[str]
    ) -> list[ParentAttentionDeliveryOutcome]: ...

    async def list_pending_evaluation(self, *, limit: int = 100) -> list[ParentAttentionDeliveryOutcome]: ...

    async def list_reconcilable(self, *, limit: int = 100) -> list[ParentAttentionDeliveryOutcome]: ...

    async def count_sent(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        chat_id: str | None = None,
    ) -> int: ...

    async def save(self, outcome: ParentAttentionDeliveryOutcome) -> None: ...


class InMemoryParentAttentionDeliveryRepository:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str], ParentAttentionDeliveryState] = {}

    async def get(self, *, child_user_id: str, chat_id: str) -> ParentAttentionDeliveryState | None:
        return self.states.get((child_user_id, chat_id))

    async def save(self, state: ParentAttentionDeliveryState) -> None:
        self.states[(state.child_user_id, state.chat_id)] = state

    async def resolve(self, *, child_user_id: str, chat_id: str, resolved_at: datetime | None = None) -> None:
        current = await self.get(child_user_id=child_user_id, chat_id=chat_id)
        if current is not None:
            self.states[(child_user_id, chat_id)] = replace(
                current, resolved_at=resolved_at or datetime.now(timezone.utc)
            )


class InMemoryParentAttentionDeliveryOutcomeRepository:
    def __init__(self) -> None:
        self.outcomes: dict[str, ParentAttentionDeliveryOutcome] = {}

    async def get(self, assessment_id: str) -> ParentAttentionDeliveryOutcome | None:
        return self.outcomes.get(assessment_id)

    async def list_for_assessments(
        self, assessment_ids: list[str]
    ) -> list[ParentAttentionDeliveryOutcome]:
        return [
            self.outcomes[assessment_id]
            for assessment_id in assessment_ids
            if assessment_id in self.outcomes
        ]

    async def list_pending_evaluation(self, *, limit: int = 100) -> list[ParentAttentionDeliveryOutcome]:
        rows = [
            outcome
            for outcome in self.outcomes.values()
            if outcome.reason == "pending_evaluation"
        ]
        rows.sort(key=lambda item: item.created_at or datetime.min.replace(tzinfo=timezone.utc))
        return rows[: max(0, limit)]

    async def list_reconcilable(
        self, *, limit: int = 100
    ) -> list[ParentAttentionDeliveryOutcome]:
        rows = [
            outcome
            for outcome in self.outcomes.values()
            if outcome.status.value == "scheduled"
        ]
        rows.sort(key=lambda item: item.updated_at or datetime.min.replace(tzinfo=timezone.utc))
        return rows[: max(0, limit)]

    async def count_sent(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        chat_id: str | None = None,
    ) -> int:
        allowed = set(child_user_ids)
        return sum(
            outcome.status.value == "sent"
            and outcome.child_user_id in allowed
            and (chat_id is None or outcome.chat_id == chat_id)
            and (created_after is None or (outcome.sent_at or datetime.min.replace(tzinfo=timezone.utc)) >= created_after)
            and (created_before is None or (outcome.sent_at or datetime.max.replace(tzinfo=timezone.utc)) < created_before)
            for outcome in self.outcomes.values()
        )

    async def save(self, outcome: ParentAttentionDeliveryOutcome) -> None:
        self.outcomes[outcome.assessment_id] = outcome


__all__ = [
    "InMemoryParentAttentionDeliveryOutcomeRepository",
    "InMemoryParentAttentionDeliveryRepository",
    "ParentAttentionDeliveryOutcomeRepository",
    "ParentAttentionDeliveryRepository",
]
