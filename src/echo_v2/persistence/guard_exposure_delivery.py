"""Persistence contracts for isolated Exposure notification delivery."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Protocol

from echo_v2.domain.parent_attention import ParentAttentionOutcomeStatus


@dataclass(frozen=True)
class ExposureDeliveryState:
    child_user_id: str
    chat_id: str
    last_scheduled_assessment_id: str | None = None
    last_scheduled_evidence_ids: tuple[str, ...] = ()
    last_action_id: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ExposureDeliveryOutcome:
    assessment_id: str
    child_user_id: str
    chat_id: str
    evidence_message_ids: tuple[str, ...]
    status: ParentAttentionOutcomeStatus
    reason: str
    scheduled_action_id: str | None = None
    provider_message_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    scheduled_at: datetime | None = None
    sent_at: datetime | None = None
    failed_at: datetime | None = None


class ExposureDeliveryRepository(Protocol):
    async def get_state(self, *, child_user_id: str, chat_id: str) -> ExposureDeliveryState | None: ...

    async def save_state(self, state: ExposureDeliveryState) -> None: ...

    async def reserve_state(self, state: ExposureDeliveryState) -> bool: ...

    async def get_outcome(self, assessment_id: str) -> ExposureDeliveryOutcome | None: ...

    async def save_outcome(self, outcome: ExposureDeliveryOutcome) -> None: ...

    async def list_overlapping(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        evidence_message_ids: tuple[str, ...],
        pending_only: bool = False,
    ) -> list[ExposureDeliveryOutcome]: ...

    async def list_reconcilable(self, *, limit: int = 100) -> list[ExposureDeliveryOutcome]: ...


class InMemoryExposureDeliveryRepository:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str], ExposureDeliveryState] = {}
        self.outcomes: dict[str, ExposureDeliveryOutcome] = {}

    async def get_state(self, *, child_user_id: str, chat_id: str) -> ExposureDeliveryState | None:
        return self.states.get((child_user_id, chat_id))

    async def save_state(self, state: ExposureDeliveryState) -> None:
        self.states[(state.child_user_id, state.chat_id)] = replace(
            state, updated_at=state.updated_at or datetime.now(timezone.utc)
        )

    async def reserve_state(self, state: ExposureDeliveryState) -> bool:
        current = self.states.get((state.child_user_id, state.chat_id))
        if current is not None and set(current.last_scheduled_evidence_ids).intersection(
            state.last_scheduled_evidence_ids
        ):
            return False
        await self.save_state(state)
        return True

    async def get_outcome(self, assessment_id: str) -> ExposureDeliveryOutcome | None:
        return self.outcomes.get(assessment_id)

    async def save_outcome(self, outcome: ExposureDeliveryOutcome) -> None:
        self.outcomes[outcome.assessment_id] = outcome

    async def list_overlapping(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        evidence_message_ids: tuple[str, ...],
        pending_only: bool = False,
    ) -> list[ExposureDeliveryOutcome]:
        evidence = set(evidence_message_ids)
        return [
            outcome
            for outcome in self.outcomes.values()
            if outcome.child_user_id == child_user_id
            and outcome.chat_id == chat_id
            and evidence.intersection(outcome.evidence_message_ids)
            and (not pending_only or outcome.status is ParentAttentionOutcomeStatus.SCHEDULED)
        ]

    async def list_reconcilable(self, *, limit: int = 100) -> list[ExposureDeliveryOutcome]:
        rows = [
            outcome for outcome in self.outcomes.values()
            if outcome.status is ParentAttentionOutcomeStatus.SCHEDULED
        ]
        rows.sort(key=lambda item: item.updated_at or datetime.min.replace(tzinfo=timezone.utc))
        return rows[:max(0, limit)]


__all__ = [
    "ExposureDeliveryOutcome",
    "ExposureDeliveryRepository",
    "ExposureDeliveryState",
    "InMemoryExposureDeliveryRepository",
]
