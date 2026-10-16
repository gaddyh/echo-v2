"""PostgreSQL persistence for isolated Baseline B assessment history."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import CursorResult, desc, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
from echo_v2.persistence.orm import GuardChatStateRow, GuardSimpleAssessmentRow


class PostgresGuardSimpleAssessmentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, assessment: SafetyAssessment) -> str:
        values: dict[str, Any] = {
            "child_user_id": assessment.child_user_id,
            "connection_id": assessment.connection_id,
            "chat_id": assessment.chat_id,
            "chat_type": assessment.chat_type,
            "chat_name": assessment.chat_name,
            "timezone": assessment.timezone,
            "window_start": assessment.window_start,
            "window_end": assessment.window_end,
            "parent_should_know": assessment.parent_should_know,
            "child_role": assessment.child_role.value,
            "summary": assessment.summary,
            "reason": assessment.reason,
            "evidence_message_ids": list(assessment.evidence_message_ids),
            "model": assessment.model,
            "prompt_version": assessment.prompt_version,
            "analyzer_version": assessment.analyzer_version,
            "truncated": assessment.truncated,
            "omitted_message_count": assessment.omitted_message_count,
            "identity_verified": assessment.identity_verified,
            "identity_reason": assessment.identity_reason,
            "diagnostics": assessment.diagnostics,
            "created_at": assessment.created_at or datetime.now(timezone.utc),
            "first_detected_at": assessment.first_detected_at,
            "last_evidence_at": assessment.last_evidence_at,
        }
        if assessment.id is not None:
            values["id"] = assessment.id
        async with self._session_factory() as session:
            result = await session.execute(
                pg_insert(GuardSimpleAssessmentRow).values(values).returning(
                    GuardSimpleAssessmentRow.id
                )
            )
            assessment_id = str(result.scalar_one())
            await session.commit()
            return assessment_id

    async def list_for_chat(
        self, *, child_user_id: str, chat_id: str, limit: int = 100
    ) -> list[SafetyAssessment]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(GuardSimpleAssessmentRow)
                    .where(
                        GuardSimpleAssessmentRow.child_user_id == child_user_id,
                        GuardSimpleAssessmentRow.chat_id == chat_id,
                    )
                    .order_by(
                        desc(GuardSimpleAssessmentRow.created_at),
                        desc(GuardSimpleAssessmentRow.id),
                    )
                    .limit(max(0, limit))
                )
            ).scalars().all()
            return [_to_domain(row) for row in reversed(rows)]

    async def latest_unresolved(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        before: datetime | None = None,
    ) -> SafetyAssessment | None:
        async with self._session_factory() as session:
            filters = [
                GuardSimpleAssessmentRow.child_user_id == child_user_id,
                GuardSimpleAssessmentRow.chat_id == chat_id,
                GuardSimpleAssessmentRow.parent_should_know.is_(True),
                GuardSimpleAssessmentRow.resolved_at.is_(None),
            ]
            if before is not None:
                filters.append(GuardSimpleAssessmentRow.created_at < before)
            row = (
                await session.execute(
                    select(GuardSimpleAssessmentRow)
                    .where(*filters)
                    .order_by(
                        GuardSimpleAssessmentRow.created_at.desc(),
                        GuardSimpleAssessmentRow.id.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            return _to_domain(row) if row is not None else None

    async def resolve(
        self, *, assessment_id: str, resolved_by: str, resolved_at: datetime | None = None
    ) -> bool:
        async with self._session_factory() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(GuardSimpleAssessmentRow)
                    .where(
                        GuardSimpleAssessmentRow.id == assessment_id,
                        GuardSimpleAssessmentRow.resolved_at.is_(None),
                    )
                    .values(
                        resolved_at=resolved_at or datetime.now(timezone.utc),
                        resolved_by=resolved_by,
                    )
                ),
            )
            await session.commit()
            return result.rowcount > 0

    async def commit_if_current(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        target_version: int,
        assessment: SafetyAssessment,
    ) -> tuple[str, str | None]:
        values: dict[str, Any] = {
            "child_user_id": assessment.child_user_id,
            "connection_id": assessment.connection_id,
            "chat_id": assessment.chat_id,
            "chat_type": assessment.chat_type,
            "chat_name": assessment.chat_name,
            "timezone": assessment.timezone,
            "window_start": assessment.window_start,
            "window_end": assessment.window_end,
            "parent_should_know": assessment.parent_should_know,
            "child_role": assessment.child_role.value,
            "summary": assessment.summary,
            "reason": assessment.reason,
            "evidence_message_ids": list(assessment.evidence_message_ids),
            "model": assessment.model,
            "prompt_version": assessment.prompt_version,
            "analyzer_version": assessment.analyzer_version,
            "truncated": assessment.truncated,
            "omitted_message_count": assessment.omitted_message_count,
            "identity_verified": assessment.identity_verified,
            "identity_reason": assessment.identity_reason,
            "diagnostics": assessment.diagnostics,
            "created_at": assessment.created_at or datetime.now(timezone.utc),
            "first_detected_at": assessment.first_detected_at,
            "last_evidence_at": assessment.last_evidence_at,
        }
        if assessment.id is not None:
            values["id"] = assessment.id
        async with self._session_factory() as session:
            state = (
                await session.execute(
                    select(GuardChatStateRow)
                    .where(
                        GuardChatStateRow.child_user_id == child_user_id,
                        GuardChatStateRow.chat_id == chat_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if state is None or state.activity_version != target_version:
                await session.rollback()
                return "stale", None
            result_id = (
                await session.execute(
                    pg_insert(GuardSimpleAssessmentRow)
                    .values(values)
                    .returning(GuardSimpleAssessmentRow.id)
                )
            ).scalar_one()
            updated = cast(
                CursorResult[Any],
                await session.execute(
                    update(GuardChatStateRow)
                .where(
                    GuardChatStateRow.child_user_id == child_user_id,
                    GuardChatStateRow.chat_id == chat_id,
                    GuardChatStateRow.activity_version == target_version,
                )
                .values(
                    last_analyzed_version=target_version,
                    last_parent_should_know=assessment.parent_should_know,
                    last_analysis_at=datetime.now(timezone.utc),
                    pending_since=None,
                    next_analysis_at=None,
                    next_analysis_reason=None,
                    updated_at=datetime.now(timezone.utc),
                )
                ),
            )
            if updated.rowcount != 1:
                await session.rollback()
                return "stale", None
            await session.commit()
            return "committed", str(result_id)


def _to_domain(row: GuardSimpleAssessmentRow) -> SafetyAssessment:
    return SafetyAssessment(
        id=str(row.id),
        child_user_id=str(row.child_user_id),
        connection_id=str(row.connection_id),
        chat_id=row.chat_id,
        chat_type=row.chat_type,
        chat_name=row.chat_name,
        timezone=row.timezone,
        window_start=row.window_start,
        window_end=row.window_end,
        parent_should_know=row.parent_should_know,
        child_role=ChildRole(row.child_role),
        summary=row.summary,
        reason=row.reason,
        evidence_message_ids=tuple(row.evidence_message_ids),
        model=row.model,
        prompt_version=row.prompt_version,
        analyzer_version=row.analyzer_version,
        truncated=row.truncated,
        omitted_message_count=row.omitted_message_count,
        identity_verified=row.identity_verified,
        identity_reason=row.identity_reason,
        diagnostics=row.diagnostics or {},
        created_at=row.created_at,
        first_detected_at=row.first_detected_at,
        last_evidence_at=row.last_evidence_at,
        resolved_at=row.resolved_at,
        resolved_by=row.resolved_by,
    )


__all__ = ["PostgresGuardSimpleAssessmentRepository"]
