"""PostgreSQL persistence for Exposure Guard shadow assessments."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.guard_exposure import ExposureAssessment, ExposureCategory
from echo_v2.persistence.orm import GuardChatStateRow, GuardExposureAssessmentRow


class PostgresGuardExposureAssessmentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, assessment: ExposureAssessment) -> str:
        values: dict[str, Any] = {
            "child_user_id": assessment.child_user_id,
            "connection_id": assessment.connection_id,
            "chat_id": assessment.chat_id,
            "chat_type": assessment.chat_type,
            "chat_name": assessment.chat_name,
            "timezone": assessment.timezone,
            "window_start": assessment.window_start,
            "window_end": assessment.window_end,
            "target_version": assessment.target_version,
            "significant_exposure": assessment.significant_exposure,
            "category": assessment.category.value if assessment.category else None,
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
            "diagnostics": _json_safe(assessment.diagnostics),
            "created_at": assessment.created_at or datetime.now(timezone.utc),
        }
        if assessment.id is not None:
            values["id"] = assessment.id
        async with self._session_factory() as session:
            result = await session.execute(
                pg_insert(GuardExposureAssessmentRow).values(values).returning(GuardExposureAssessmentRow.id)
            )
            assessment_id = str(result.scalar_one())
            await session.commit()
            return assessment_id

    async def get(self, assessment_id: str) -> ExposureAssessment | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(GuardExposureAssessmentRow).where(GuardExposureAssessmentRow.id == assessment_id)
                )
            ).scalar_one_or_none()
            return _to_domain(row) if row is not None else None

    async def list_for_children(
        self, *, child_user_ids: list[str], created_after: datetime | None = None,
        created_before: datetime | None = None, chat_id: str | None = None,
        decision: str | None = None, limit: int = 50,
        before: tuple[datetime, str] | None = None,
    ) -> list[ExposureAssessment]:
        if not child_user_ids or limit <= 0:
            return []
        filters: list[Any] = [GuardExposureAssessmentRow.child_user_id.in_(child_user_ids)]
        if created_after is not None:
            filters.append(GuardExposureAssessmentRow.created_at >= created_after)
        if created_before is not None:
            filters.append(GuardExposureAssessmentRow.created_at < created_before)
        if chat_id is not None:
            filters.append(GuardExposureAssessmentRow.chat_id == chat_id)
        if decision == "yes":
            filters.append(GuardExposureAssessmentRow.significant_exposure.is_(True))
        elif decision == "no":
            filters.append(GuardExposureAssessmentRow.significant_exposure.is_(False))
        elif decision == "unknown":
            filters.append(GuardExposureAssessmentRow.significant_exposure.is_(None))
        if before is not None:
            before_at, before_id = before
            filters.append(or_(
                GuardExposureAssessmentRow.created_at < before_at,
                and_(GuardExposureAssessmentRow.created_at == before_at, GuardExposureAssessmentRow.id < before_id),
            ))
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(GuardExposureAssessmentRow)
                    .where(*filters)
                    .order_by(GuardExposureAssessmentRow.created_at.desc(), GuardExposureAssessmentRow.id.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [_to_domain(row) for row in rows]

    async def count_for_children(
        self, *, child_user_ids: list[str], created_after: datetime | None = None,
        created_before: datetime | None = None, chat_id: str | None = None,
        decision: str | None = None,
    ) -> dict[str, int]:
        rows = await self.list_for_children(
            child_user_ids=child_user_ids, created_after=created_after,
            created_before=created_before, chat_id=chat_id, decision=decision,
            limit=1_000_000,
        )
        return {
            "analyzed": len(rows),
            "yes": sum(row.significant_exposure is True for row in rows),
            "no": sum(row.significant_exposure is False for row in rows),
            "unknown": sum(row.significant_exposure is None for row in rows),
        }

    async def commit_if_current(
        self, *, child_user_id: str, chat_id: str, target_version: int,
        assessment: ExposureAssessment,
    ) -> tuple[str, str | None]:
        async with self._session_factory() as session:
            state = (
                await session.execute(
                    select(GuardChatStateRow.activity_version).where(
                        GuardChatStateRow.child_user_id == child_user_id,
                        GuardChatStateRow.chat_id == chat_id,
                    )
                )
            ).scalar_one_or_none()
            if state != target_version:
                return "stale", None
            values = replace(assessment, target_version=target_version)
            assessment_id = await _save_in_session(session, values)
            await session.commit()
            return "committed", assessment_id


async def _save_in_session(session: AsyncSession, assessment: ExposureAssessment) -> str:
    values: dict[str, Any] = {
        "child_user_id": assessment.child_user_id,
        "connection_id": assessment.connection_id,
        "chat_id": assessment.chat_id,
        "chat_type": assessment.chat_type,
        "chat_name": assessment.chat_name,
        "timezone": assessment.timezone,
        "window_start": assessment.window_start,
        "window_end": assessment.window_end,
        "target_version": assessment.target_version,
        "significant_exposure": assessment.significant_exposure,
        "category": assessment.category.value if assessment.category else None,
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
        "diagnostics": _json_safe(assessment.diagnostics),
        "created_at": assessment.created_at or datetime.now(timezone.utc),
    }
    if assessment.id is not None:
        values["id"] = assessment.id
    result = await session.execute(
        pg_insert(GuardExposureAssessmentRow).values(values).returning(GuardExposureAssessmentRow.id)
    )
    return str(result.scalar_one())


def _to_domain(row: GuardExposureAssessmentRow) -> ExposureAssessment:
    return ExposureAssessment(
        id=str(row.id), child_user_id=str(row.child_user_id), connection_id=str(row.connection_id),
        chat_id=row.chat_id, chat_type=row.chat_type, chat_name=row.chat_name, timezone=row.timezone,
        window_start=row.window_start, window_end=row.window_end, target_version=row.target_version,
        significant_exposure=row.significant_exposure,
        category=ExposureCategory(row.category) if row.category else None,
        summary=row.summary, reason=row.reason,
        evidence_message_ids=tuple(row.evidence_message_ids),
        input_message_ids=tuple(row.input_message_ids or ()),
        input_content_hash=row.input_content_hash, model=row.model,
        prompt_version=row.prompt_version, analyzer_version=row.analyzer_version,
        truncated=row.truncated, omitted_message_count=row.omitted_message_count,
        diagnostics=row.diagnostics or {}, created_at=row.created_at,
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


__all__ = ["PostgresGuardExposureAssessmentRepository"]
