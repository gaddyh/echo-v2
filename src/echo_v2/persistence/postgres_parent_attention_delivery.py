"""PostgreSQL persistence for parent-attention delivery history."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.parent_attention import (
    ParentAttentionDeliveryOutcome,
    ParentAttentionDeliveryState,
    ParentAttentionOutcomeStatus,
)
from echo_v2.persistence.orm import (
    GuardParentAttentionDeliveryOutcomeRow,
    GuardParentAttentionDeliveryRow,
    GuardSimpleAssessmentRow,
)


class PostgresParentAttentionDeliveryRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, *, child_user_id: str, chat_id: str) -> ParentAttentionDeliveryState | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(GuardParentAttentionDeliveryRow).where(
                        GuardParentAttentionDeliveryRow.child_user_id == child_user_id,
                        GuardParentAttentionDeliveryRow.chat_id == chat_id,
                    )
                )
            ).scalar_one_or_none()
            return _to_domain(row) if row is not None else None

    async def save(self, state: ParentAttentionDeliveryState) -> None:
        values: dict[str, Any] = {
            "child_user_id": state.child_user_id,
            "chat_id": state.chat_id,
            "last_delivered_assessment_id": state.last_delivered_assessment_id,
            "last_delivered_evidence_ids": list(state.last_delivered_evidence_ids),
            "last_scheduled_at": state.last_scheduled_at,
            "last_action_id": state.last_action_id,
            "resolved_at": state.resolved_at,
            "updated_at": datetime.now(timezone.utc),
        }
        async with self._session_factory() as session:
            await session.execute(
                pg_insert(GuardParentAttentionDeliveryRow)
                .values(values)
                .on_conflict_do_update(
                    index_elements=["child_user_id", "chat_id"],
                    set_=values,
                )
            )
            await session.commit()

    async def resolve(
        self, *, child_user_id: str, chat_id: str, resolved_at: datetime | None = None
    ) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(GuardParentAttentionDeliveryRow)
                .where(
                    GuardParentAttentionDeliveryRow.child_user_id == child_user_id,
                    GuardParentAttentionDeliveryRow.chat_id == chat_id,
                )
                .values(
                    resolved_at=resolved_at or datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()


class PostgresParentAttentionDeliveryOutcomeRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, assessment_id: str) -> ParentAttentionDeliveryOutcome | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(GuardParentAttentionDeliveryOutcomeRow).where(
                        GuardParentAttentionDeliveryOutcomeRow.assessment_id == assessment_id
                    )
                )
            ).scalar_one_or_none()
            return _outcome_to_domain(row) if row is not None else None

    async def list_for_assessments(
        self, assessment_ids: list[str]
    ) -> list[ParentAttentionDeliveryOutcome]:
        if not assessment_ids:
            return []
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(GuardParentAttentionDeliveryOutcomeRow).where(
                        GuardParentAttentionDeliveryOutcomeRow.assessment_id.in_(assessment_ids)
                    )
                )
            ).scalars().all()
            return [_outcome_to_domain(row) for row in rows]

    async def list_pending_evaluation(
        self, *, limit: int = 100
    ) -> list[ParentAttentionDeliveryOutcome]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(GuardParentAttentionDeliveryOutcomeRow)
                    .where(
                        GuardParentAttentionDeliveryOutcomeRow.status
                        == ParentAttentionOutcomeStatus.NOT_SENT.value,
                        GuardParentAttentionDeliveryOutcomeRow.reason == "pending_evaluation",
                    )
                    .order_by(GuardParentAttentionDeliveryOutcomeRow.created_at)
                    .limit(max(0, limit))
                )
            ).scalars().all()
            return [_outcome_to_domain(row) for row in rows]

    async def list_reconcilable(
        self, *, limit: int = 100
    ) -> list[ParentAttentionDeliveryOutcome]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(GuardParentAttentionDeliveryOutcomeRow)
                    .where(
                        GuardParentAttentionDeliveryOutcomeRow.status
                        == ParentAttentionOutcomeStatus.SCHEDULED.value
                    )
                    .order_by(GuardParentAttentionDeliveryOutcomeRow.updated_at)
                    .limit(max(0, limit))
                )
            ).scalars().all()
            return [_outcome_to_domain(row) for row in rows]

    async def count_sent(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        chat_id: str | None = None,
    ) -> int:
        if not child_user_ids:
            return 0
        filters: list[Any] = [
            GuardParentAttentionDeliveryOutcomeRow.child_user_id.in_(child_user_ids),
            GuardParentAttentionDeliveryOutcomeRow.status == ParentAttentionOutcomeStatus.SENT.value,
        ]
        if created_after is not None:
            filters.append(GuardSimpleAssessmentRow.created_at >= created_after)
        if created_before is not None:
            filters.append(GuardSimpleAssessmentRow.created_at < created_before)
        if chat_id is not None:
            filters.append(GuardParentAttentionDeliveryOutcomeRow.chat_id == chat_id)
        async with self._session_factory() as session:
            count = await session.execute(
                select(func.count())
                .select_from(GuardParentAttentionDeliveryOutcomeRow)
                .join(
                    GuardSimpleAssessmentRow,
                    GuardSimpleAssessmentRow.id
                    == GuardParentAttentionDeliveryOutcomeRow.assessment_id,
                )
                .where(*filters)
            )
            return int(count.scalar_one())

    async def save(self, outcome: ParentAttentionDeliveryOutcome) -> None:
        now = datetime.now(timezone.utc)
        values: dict[str, Any] = {
            "assessment_id": outcome.assessment_id,
            "child_user_id": outcome.child_user_id,
            "chat_id": outcome.chat_id,
            "status": outcome.status.value,
            "reason": outcome.reason,
            "scheduled_action_id": outcome.scheduled_action_id,
            "provider_message_id": outcome.provider_message_id,
            "created_at": outcome.created_at or now,
            "updated_at": now,
            "scheduled_at": outcome.scheduled_at,
            "sent_at": outcome.sent_at,
            "failed_at": outcome.failed_at,
        }
        async with self._session_factory() as session:
            await session.execute(
                pg_insert(GuardParentAttentionDeliveryOutcomeRow)
                .values(values)
                .on_conflict_do_update(
                    index_elements=["assessment_id"],
                    set_={key: value for key, value in values.items() if key not in {"assessment_id", "created_at"}},
                )
            )
            await session.commit()


def _to_domain(row: GuardParentAttentionDeliveryRow) -> ParentAttentionDeliveryState:
    return ParentAttentionDeliveryState(
        child_user_id=str(row.child_user_id),
        chat_id=row.chat_id,
        last_delivered_assessment_id=(
            str(row.last_delivered_assessment_id)
            if row.last_delivered_assessment_id is not None
            else None
        ),
        last_delivered_evidence_ids=tuple(row.last_delivered_evidence_ids),
        last_scheduled_at=row.last_scheduled_at,
        last_action_id=str(row.last_action_id) if row.last_action_id is not None else None,
        resolved_at=row.resolved_at,
    )


def _outcome_to_domain(row: GuardParentAttentionDeliveryOutcomeRow) -> ParentAttentionDeliveryOutcome:
    return ParentAttentionDeliveryOutcome(
        assessment_id=str(row.assessment_id),
        child_user_id=str(row.child_user_id),
        chat_id=row.chat_id,
        status=ParentAttentionOutcomeStatus(row.status),
        reason=row.reason,
        scheduled_action_id=(
            str(row.scheduled_action_id) if row.scheduled_action_id is not None else None
        ),
        provider_message_id=row.provider_message_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        scheduled_at=row.scheduled_at,
        sent_at=row.sent_at,
        failed_at=row.failed_at,
    )


__all__ = [
    "PostgresParentAttentionDeliveryOutcomeRepository",
    "PostgresParentAttentionDeliveryRepository",
]
