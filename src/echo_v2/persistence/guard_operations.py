"""Persistence for Guard operations telemetry."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.guard_operations import (
    GuardAnalysisAttempt,
    GuardAnalysisAttemptStatus,
)
from echo_v2.persistence.orm import GuardAnalysisAttemptRow


@runtime_checkable
class GuardAnalysisTelemetryRepository(Protocol):
    async def start(self, attempt: GuardAnalysisAttempt) -> None: ...

    async def finish(
        self,
        attempt_id: str,
        *,
        status: GuardAnalysisAttemptStatus,
        finished_at: datetime,
        result_id: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        analyzer_version: str | None = None,
        error_code: str | None = None,
    ) -> None: ...

    async def list_for_chat(
        self, *, child_user_id: str, chat_id: str, limit: int = 500
    ) -> list[GuardAnalysisAttempt]: ...


class PostgresGuardAnalysisTelemetryRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def start(self, attempt: GuardAnalysisAttempt) -> None:
        async with self._session_factory() as session:
            session.add(
                GuardAnalysisAttemptRow(
                    id=attempt.id,
                    child_user_id=attempt.child_user_id,
                    chat_id=attempt.chat_id,
                    target_version=attempt.target_version,
                    scheduled_for=attempt.scheduled_for,
                    pending_since=attempt.pending_since,
                    status=attempt.status.value,
                    started_at=attempt.started_at,
                    finished_at=attempt.finished_at,
                    result_id=attempt.result_id,
                    model=attempt.model,
                    prompt_version=attempt.prompt_version,
                    analyzer_version=attempt.analyzer_version,
                    error_code=attempt.error_code,
                )
            )
            await session.commit()

    async def finish(
        self,
        attempt_id: str,
        *,
        status: GuardAnalysisAttemptStatus,
        finished_at: datetime,
        result_id: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        analyzer_version: str | None = None,
        error_code: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(GuardAnalysisAttemptRow)
                .where(GuardAnalysisAttemptRow.id == attempt_id)
                .values(
                    status=status.value,
                    finished_at=finished_at,
                    result_id=result_id,
                    model=model,
                    prompt_version=prompt_version,
                    analyzer_version=analyzer_version,
                    error_code=error_code,
                )
            )
            await session.commit()

    async def list_for_chat(
        self, *, child_user_id: str, chat_id: str, limit: int = 500
    ) -> list[GuardAnalysisAttempt]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(GuardAnalysisAttemptRow)
                    .where(
                        GuardAnalysisAttemptRow.child_user_id == child_user_id,
                        GuardAnalysisAttemptRow.chat_id == chat_id,
                    )
                    .order_by(GuardAnalysisAttemptRow.started_at)
                    .limit(limit)
                )
            ).scalars().all()
            return [_to_domain(row) for row in rows]


class InMemoryGuardAnalysisTelemetryRepository:
    def __init__(self) -> None:
        self.attempts: dict[str, GuardAnalysisAttempt] = {}

    async def start(self, attempt: GuardAnalysisAttempt) -> None:
        self.attempts[attempt.id] = attempt

    async def finish(
        self,
        attempt_id: str,
        *,
        status: GuardAnalysisAttemptStatus,
        finished_at: datetime,
        result_id: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        analyzer_version: str | None = None,
        error_code: str | None = None,
    ) -> None:
        attempt = self.attempts[attempt_id]
        self.attempts[attempt_id] = replace(
            attempt,
            status=status,
            finished_at=finished_at,
            result_id=result_id,
            model=model,
            prompt_version=prompt_version,
            analyzer_version=analyzer_version,
            error_code=error_code,
        )

    async def list_for_chat(
        self, *, child_user_id: str, chat_id: str, limit: int = 500
    ) -> list[GuardAnalysisAttempt]:
        rows = [
            attempt for attempt in self.attempts.values()
            if attempt.child_user_id == child_user_id and attempt.chat_id == chat_id
        ]
        rows.sort(key=lambda item: item.started_at)
        return rows[-limit:]


def _to_domain(row: GuardAnalysisAttemptRow) -> GuardAnalysisAttempt:
    return GuardAnalysisAttempt(
        id=str(row.id),
        child_user_id=str(row.child_user_id),
        chat_id=row.chat_id,
        target_version=row.target_version,
        status=GuardAnalysisAttemptStatus(row.status),
        started_at=row.started_at,
        scheduled_for=row.scheduled_for,
        pending_since=row.pending_since,
        finished_at=row.finished_at,
        result_id=str(row.result_id) if row.result_id else None,
        model=row.model,
        prompt_version=row.prompt_version,
        analyzer_version=row.analyzer_version,
        error_code=row.error_code,
    )


__all__ = [
    "GuardAnalysisTelemetryRepository",
    "InMemoryGuardAnalysisTelemetryRepository",
    "PostgresGuardAnalysisTelemetryRepository",
]
