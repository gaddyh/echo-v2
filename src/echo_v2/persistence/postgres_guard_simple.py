"""PostgreSQL persistence for isolated Baseline B assessment history."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import CursorResult, and_, desc, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
from echo_v2.persistence.orm import (
    GuardChatStateRow,
    GuardSimpleAssessmentRow,
)


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
            "input_message_ids": list(assessment.input_message_ids),
            "input_content_hash": assessment.input_content_hash,
            "model": assessment.model,
            "prompt_version": assessment.prompt_version,
            "analyzer_version": assessment.analyzer_version,
            "truncated": assessment.truncated,
            "omitted_message_count": assessment.omitted_message_count,
            "identity_verified": assessment.identity_verified,
            "identity_reason": assessment.identity_reason,
            "diagnostics": _json_safe_diagnostics(assessment.diagnostics),
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

    async def get(self, assessment_id: str) -> SafetyAssessment | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(GuardSimpleAssessmentRow).where(
                        GuardSimpleAssessmentRow.id == assessment_id
                    )
                )
            ).scalar_one_or_none()
            return _to_domain(row) if row is not None else None

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

    async def list_for_children(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        chat_id: str | None = None,
        chat_name: str | None = None,
        decision: str | None = None,
        limit: int = 50,
        before: tuple[datetime, str] | None = None,
    ) -> list[SafetyAssessment]:
        if not child_user_ids or limit <= 0:
            return []
        filters: list[Any] = [GuardSimpleAssessmentRow.child_user_id.in_(child_user_ids)]
        if created_after is not None:
            filters.append(GuardSimpleAssessmentRow.created_at >= created_after)
        if created_before is not None:
            filters.append(GuardSimpleAssessmentRow.created_at < created_before)
        if chat_id is not None:
            filters.append(GuardSimpleAssessmentRow.chat_id == chat_id)
        if chat_name and chat_name.strip():
            filters.append(GuardSimpleAssessmentRow.chat_name.icontains(chat_name.strip(), autoescape=True))
        if decision == "yes":
            filters.append(GuardSimpleAssessmentRow.parent_should_know.is_(True))
        elif decision == "no":
            filters.append(GuardSimpleAssessmentRow.parent_should_know.is_(False))
        elif decision == "unknown":
            filters.append(GuardSimpleAssessmentRow.parent_should_know.is_(None))
        if before is not None:
            before_at, before_id = before
            filters.append(
                or_(
                    GuardSimpleAssessmentRow.created_at < before_at,
                    and_(
                        GuardSimpleAssessmentRow.created_at == before_at,
                        GuardSimpleAssessmentRow.id < before_id,
                    ),
                )
            )
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(GuardSimpleAssessmentRow)
                    .where(*filters)
                    .order_by(
                        GuardSimpleAssessmentRow.created_at.desc(),
                        GuardSimpleAssessmentRow.id.desc(),
                    )
                    .limit(limit)
                )
            ).scalars().all()
            return [_to_domain(row) for row in rows]

    async def count_for_children(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        chat_id: str | None = None,
        chat_name: str | None = None,
        decision: str | None = None,
    ) -> dict[str, int]:
        if not child_user_ids:
            return {"analyzed": 0, "yes": 0, "no": 0, "unknown": 0}
        filters: list[Any] = [GuardSimpleAssessmentRow.child_user_id.in_(child_user_ids)]
        if created_after is not None:
            filters.append(GuardSimpleAssessmentRow.created_at >= created_after)
        if created_before is not None:
            filters.append(GuardSimpleAssessmentRow.created_at < created_before)
        if chat_id is not None:
            filters.append(GuardSimpleAssessmentRow.chat_id == chat_id)
        if chat_name and chat_name.strip():
            filters.append(GuardSimpleAssessmentRow.chat_name.icontains(chat_name.strip(), autoescape=True))
        if decision == "yes":
            filters.append(GuardSimpleAssessmentRow.parent_should_know.is_(True))
        elif decision == "no":
            filters.append(GuardSimpleAssessmentRow.parent_should_know.is_(False))
        elif decision == "unknown":
            filters.append(GuardSimpleAssessmentRow.parent_should_know.is_(None))
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(GuardSimpleAssessmentRow.parent_should_know)
                    .where(*filters)
                )
            ).scalars().all()
            return {
                "analyzed": len(rows),
                "yes": sum(value is True for value in rows),
                "no": sum(value is False for value in rows),
                "unknown": sum(value is None for value in rows),
            }

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
            "input_message_ids": list(assessment.input_message_ids),
            "input_content_hash": assessment.input_content_hash,
            "model": assessment.model,
            "prompt_version": assessment.prompt_version,
            "analyzer_version": assessment.analyzer_version,
            "truncated": assessment.truncated,
            "omitted_message_count": assessment.omitted_message_count,
            "identity_verified": assessment.identity_verified,
            "identity_reason": assessment.identity_reason,
            "diagnostics": _json_safe_diagnostics(assessment.diagnostics),
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
            now = datetime.now(timezone.utc)
            retry_at_value = assessment.diagnostics.get("next_media_retry_at")
            if isinstance(retry_at_value, str):
                retry_at_value = datetime.fromisoformat(retry_at_value)
            retry_at = retry_at_value
            retryable = (
                assessment.diagnostics.get("retryable_media_evidence") is True
                and isinstance(retry_at, datetime)
            )
            values_for_state: dict[str, Any] = {
                "last_parent_should_know": assessment.parent_should_know,
                "last_analysis_at": now,
                "updated_at": now,
            }
            if retryable:
                values_for_state.update(
                    next_analysis_at=retry_at,
                    next_analysis_reason="incomplete_media_evidence",
                )
            else:
                values_for_state.update(
                    last_analyzed_version=target_version,
                    pending_since=None,
                    next_analysis_at=None,
                    next_analysis_reason=None,
                )
            updated = cast(
                CursorResult[Any],
                await session.execute(
                    update(GuardChatStateRow)
                    .where(
                        GuardChatStateRow.child_user_id == child_user_id,
                        GuardChatStateRow.chat_id == chat_id,
                        GuardChatStateRow.activity_version == target_version,
                    )
                    .values(**values_for_state)
                ),
            )
            if updated.rowcount != 1:
                await session.rollback()
                return "stale", None
            await session.commit()
            return ("deferred" if retryable else "committed"), str(result_id)


def _json_safe_diagnostics(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe_diagnostics(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_diagnostics(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_diagnostics(item) for item in value]
    return value


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
        input_message_ids=tuple(row.input_message_ids or ()),
        input_content_hash=row.input_content_hash,
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
