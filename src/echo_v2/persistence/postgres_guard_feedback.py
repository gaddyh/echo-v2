"""PostgreSQL persistence for Guard operator feedback."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.guard_feedback import (
    GuardAnalysisFeedback,
    GuardFeedbackEvent,
    GuardFeedbackLabel,
)
from echo_v2.persistence.orm import (
    GuardAnalysisFeedbackEventRow,
    GuardAnalysisFeedbackRow,
    GuardAnalysisResultRow,
)


class PostgresGuardFeedbackRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def set_feedback(
        self, *, result_id: str, reviewer_user_id: str, label: GuardFeedbackLabel, note: str | None
    ) -> GuardAnalysisFeedback | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(GuardAnalysisResultRow.id).where(GuardAnalysisResultRow.id == result_id)
            )
            if result.scalar_one_or_none() is None:
                await session.rollback()
                return None
            current_row = (
                await session.execute(
                    select(GuardAnalysisFeedbackRow)
                    .where(GuardAnalysisFeedbackRow.result_id == result_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            now = datetime.now(timezone.utc)
            await session.execute(
                pg_insert(GuardAnalysisFeedbackEventRow).values(
                    result_id=result_id,
                    reviewer_user_id=reviewer_user_id,
                    old_label=current_row.label if current_row else None,
                    new_label=label.value,
                    old_note=current_row.note if current_row else None,
                    new_note=note,
                    changed_at=now,
                )
            )
            stmt = pg_insert(GuardAnalysisFeedbackRow).values(
                result_id=result_id, reviewer_user_id=reviewer_user_id,
                label=label.value, note=note, created_at=now, updated_at=now,
            ).on_conflict_do_update(
                index_elements=["result_id"],
                set_={
                    "reviewer_user_id": reviewer_user_id,
                    "label": label.value,
                    "note": note,
                    "updated_at": now,
                },
            ).returning(GuardAnalysisFeedbackRow)
            row = (await session.execute(stmt.execution_options(populate_existing=True))).scalar_one()
            await session.commit()
            return self._to_feedback(row)

    async def get_for_results(self, result_ids: list[str]) -> dict[str, GuardAnalysisFeedback]:
        if not result_ids:
            return {}
        async with self._session_factory() as session:
            rows = (await session.execute(
                select(GuardAnalysisFeedbackRow).where(GuardAnalysisFeedbackRow.result_id.in_(result_ids))
            )).scalars().all()
            return {str(row.result_id): self._to_feedback(row) for row in rows}

    async def list_events(self, result_id: str) -> list[GuardFeedbackEvent]:
        async with self._session_factory() as session:
            rows = (await session.execute(
                select(GuardAnalysisFeedbackEventRow)
                .where(GuardAnalysisFeedbackEventRow.result_id == result_id)
                .order_by(GuardAnalysisFeedbackEventRow.changed_at)
            )).scalars().all()
            return [self._to_event(row) for row in rows]

    @staticmethod
    def _to_feedback(row: GuardAnalysisFeedbackRow) -> GuardAnalysisFeedback:
        return GuardAnalysisFeedback(
            result_id=str(row.result_id), reviewer_user_id=str(row.reviewer_user_id),
            label=GuardFeedbackLabel(row.label), note=row.note,
            created_at=row.created_at, updated_at=row.updated_at,
        )

    @staticmethod
    def _to_event(row: GuardAnalysisFeedbackEventRow) -> GuardFeedbackEvent:
        return GuardFeedbackEvent(
            id=str(row.id), result_id=str(row.result_id), reviewer_user_id=str(row.reviewer_user_id),
            old_label=GuardFeedbackLabel(row.old_label) if row.old_label else None,
            new_label=GuardFeedbackLabel(row.new_label), old_note=row.old_note,
            new_note=row.new_note, changed_at=row.changed_at,
        )
