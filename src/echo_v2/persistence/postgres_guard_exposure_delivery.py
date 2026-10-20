"""PostgreSQL persistence for isolated Exposure notification delivery."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.parent_attention import ParentAttentionOutcomeStatus
from echo_v2.persistence.guard_exposure_delivery import (
    ExposureDeliveryOutcome,
    ExposureDeliveryState,
)
from echo_v2.persistence.orm import (
    GuardExposureDeliveryOutcomeRow,
    GuardExposureDeliveryRow,
)


class PostgresExposureDeliveryRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_state(
        self, *, guardian_user_id: str, child_user_id: str, chat_id: str
    ) -> ExposureDeliveryState | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(GuardExposureDeliveryRow).where(
                        GuardExposureDeliveryRow.guardian_user_id == guardian_user_id,
                        GuardExposureDeliveryRow.child_user_id == child_user_id,
                        GuardExposureDeliveryRow.chat_id == chat_id,
                    )
                )
            ).scalar_one_or_none()
            return _state(row) if row is not None else None

    async def save_state(self, state: ExposureDeliveryState) -> None:
        values: dict[str, Any] = {
            "guardian_user_id": state.guardian_user_id,
            "child_user_id": state.child_user_id,
            "chat_id": state.chat_id,
            "last_scheduled_assessment_id": state.last_scheduled_assessment_id,
            "last_scheduled_evidence_ids": list(state.last_scheduled_evidence_ids),
            "last_action_id": state.last_action_id,
            "updated_at": datetime.now(timezone.utc),
        }
        async with self._session_factory() as session:
            await session.execute(
                pg_insert(GuardExposureDeliveryRow)
                .values(values)
                .on_conflict_do_update(
                    index_elements=["guardian_user_id", "child_user_id", "chat_id"],
                    set_={key: value for key, value in values.items() if key not in {"guardian_user_id", "child_user_id", "chat_id"}},
                )
            )
            await session.commit()

    async def reserve_state(self, state: ExposureDeliveryState) -> bool:
        values: dict[str, Any] = {
            "guardian_user_id": state.guardian_user_id,
            "child_user_id": state.child_user_id,
            "chat_id": state.chat_id,
            "last_scheduled_assessment_id": state.last_scheduled_assessment_id,
            "last_scheduled_evidence_ids": list(state.last_scheduled_evidence_ids),
            "last_action_id": state.last_action_id,
            "updated_at": datetime.now(timezone.utc),
        }
        async with self._session_factory() as session:
            current = (
                await session.execute(
                    select(GuardExposureDeliveryRow)
                    .where(
                        GuardExposureDeliveryRow.guardian_user_id == state.guardian_user_id,
                        GuardExposureDeliveryRow.child_user_id == state.child_user_id,
                        GuardExposureDeliveryRow.chat_id == state.chat_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if current is not None and set(current.last_scheduled_evidence_ids).intersection(
                state.last_scheduled_evidence_ids
            ):
                await session.rollback()
                return False
            await session.execute(
                pg_insert(GuardExposureDeliveryRow)
                .values(values)
                .on_conflict_do_update(
                    index_elements=["guardian_user_id", "child_user_id", "chat_id"],
                    set_={key: value for key, value in values.items() if key not in {"guardian_user_id", "child_user_id", "chat_id"}},
                )
            )
            await session.commit()
            return True

    async def get_outcome(
        self, assessment_id: str, *, guardian_user_id: str
    ) -> ExposureDeliveryOutcome | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(GuardExposureDeliveryOutcomeRow).where(
                        GuardExposureDeliveryOutcomeRow.assessment_id == assessment_id,
                        GuardExposureDeliveryOutcomeRow.guardian_user_id == guardian_user_id,
                    )
                )
            ).scalar_one_or_none()
            return _outcome(row) if row is not None else None

    async def save_outcome(self, outcome: ExposureDeliveryOutcome) -> None:
        now = datetime.now(timezone.utc)
        values: dict[str, Any] = {
            "assessment_id": outcome.assessment_id,
            "guardian_user_id": outcome.guardian_user_id,
            "child_user_id": outcome.child_user_id,
            "chat_id": outcome.chat_id,
            "evidence_message_ids": list(outcome.evidence_message_ids),
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
                pg_insert(GuardExposureDeliveryOutcomeRow)
                .values(values)
                .on_conflict_do_update(
                    index_elements=["assessment_id", "guardian_user_id"],
                    set_={key: value for key, value in values.items() if key not in {"assessment_id", "guardian_user_id", "created_at"}},
                )
            )
            await session.commit()

    async def list_overlapping(
        self,
        *,
        guardian_user_id: str,
        child_user_id: str,
        chat_id: str,
        evidence_message_ids: tuple[str, ...],
        pending_only: bool = False,
    ) -> list[ExposureDeliveryOutcome]:
        if not evidence_message_ids:
            return []
        async with self._session_factory() as session:
            filters: list[Any] = [
                GuardExposureDeliveryOutcomeRow.guardian_user_id == guardian_user_id,
                GuardExposureDeliveryOutcomeRow.child_user_id == child_user_id,
                GuardExposureDeliveryOutcomeRow.chat_id == chat_id,
            ]
            if pending_only:
                filters.append(
                    GuardExposureDeliveryOutcomeRow.status
                    == ParentAttentionOutcomeStatus.SCHEDULED.value
                )
            rows = (
                await session.execute(
                    select(GuardExposureDeliveryOutcomeRow).where(*filters)
                )
            ).scalars().all()
            return [
                item for item in (_outcome(row) for row in rows)
                if set(evidence_message_ids).intersection(item.evidence_message_ids)
            ]

    async def list_reconcilable(self, *, limit: int = 100) -> list[ExposureDeliveryOutcome]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(GuardExposureDeliveryOutcomeRow)
                    .where(
                        GuardExposureDeliveryOutcomeRow.status
                        == ParentAttentionOutcomeStatus.SCHEDULED.value
                    )
                    .order_by(GuardExposureDeliveryOutcomeRow.updated_at)
                    .limit(max(0, limit))
                )
            ).scalars().all()
            return [_outcome(row) for row in rows]


def _state(row: GuardExposureDeliveryRow) -> ExposureDeliveryState:
    return ExposureDeliveryState(
        guardian_user_id=str(row.guardian_user_id),
        child_user_id=str(row.child_user_id),
        chat_id=row.chat_id,
        last_scheduled_assessment_id=(
            str(row.last_scheduled_assessment_id)
            if row.last_scheduled_assessment_id is not None else None
        ),
        last_scheduled_evidence_ids=tuple(row.last_scheduled_evidence_ids),
        last_action_id=str(row.last_action_id) if row.last_action_id is not None else None,
        updated_at=row.updated_at,
    )


def _outcome(row: GuardExposureDeliveryOutcomeRow) -> ExposureDeliveryOutcome:
    return ExposureDeliveryOutcome(
        assessment_id=str(row.assessment_id),
        guardian_user_id=str(row.guardian_user_id),
        child_user_id=str(row.child_user_id),
        chat_id=row.chat_id,
        evidence_message_ids=tuple(row.evidence_message_ids),
        status=ParentAttentionOutcomeStatus(row.status),
        reason=row.reason,
        scheduled_action_id=str(row.scheduled_action_id) if row.scheduled_action_id else None,
        provider_message_id=row.provider_message_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        scheduled_at=row.scheduled_at,
        sent_at=row.sent_at,
        failed_at=row.failed_at,
    )


__all__ = ["PostgresExposureDeliveryRepository"]
