"""PostgreSQL persistence for parent-attention delivery history."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.parent_attention import ParentAttentionDeliveryState
from echo_v2.persistence.orm import GuardParentAttentionDeliveryRow


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
            "last_delivered_at": state.last_delivered_at,
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
        last_delivered_at=row.last_delivered_at,
        last_action_id=str(row.last_action_id) if row.last_action_id is not None else None,
        resolved_at=row.resolved_at,
    )


__all__ = ["PostgresParentAttentionDeliveryRepository"]
