"""PostgreSQL repositories for Guard links, queue state, and observations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.guard import (
    GuardAnalysisRecord,
    GuardChatState,
    GuardianChildLink,
    GuardianChildLinkStatus,
)
from echo_v2.persistence.orm import (
    GuardAnalysisResultRow,
    GuardChatStateRow,
    GuardianChildLinkRow,
)
from echo_v2.services.guard_taxonomy import GuardDecision

__all__ = [
    "PostgresGuardAnalysisCommitRepository",
    "PostgresGuardAnalysisRepository",
    "PostgresGuardChatStateRepository",
    "PostgresGuardianChildLinkRepository",
]


class PostgresGuardianChildLinkRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_active_for_child(self, child_user_id: str) -> GuardianChildLink | None:
        async with self._session_factory() as session:
            stmt = (
                select(GuardianChildLinkRow)
                .where(
                    GuardianChildLinkRow.child_user_id == child_user_id,
                    GuardianChildLinkRow.status == GuardianChildLinkStatus.ACTIVE.value,
                    GuardianChildLinkRow.safety_enabled_at.is_not(None),
                )
                .order_by(GuardianChildLinkRow.updated_at.desc())
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._to_domain(row) if row else None

    async def list_active_for_guardian(self, guardian_user_id: str) -> list[GuardianChildLink]:
        async with self._session_factory() as session:
            stmt = select(GuardianChildLinkRow).where(
                GuardianChildLinkRow.guardian_user_id == guardian_user_id,
                GuardianChildLinkRow.status == GuardianChildLinkStatus.ACTIVE.value,
                GuardianChildLinkRow.safety_enabled_at.is_not(None),
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_domain(row) for row in rows]

    async def upsert_active(
        self, *, guardian_user_id: str, child_user_id: str, now: datetime
    ) -> GuardianChildLink:
        async with self._session_factory() as session:
            stmt = (
                pg_insert(GuardianChildLinkRow)
                .values(
                    guardian_user_id=guardian_user_id,
                    child_user_id=child_user_id,
                    status=GuardianChildLinkStatus.ACTIVE.value,
                    child_consented_at=now,
                    safety_enabled_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["guardian_user_id", "child_user_id"],
                    set_={
                        "status": GuardianChildLinkStatus.ACTIVE.value,
                        "child_consented_at": now,
                        "safety_enabled_at": now,
                        "updated_at": now,
                    },
                )
                .returning(GuardianChildLinkRow)
            )
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
            return self._to_domain(row)

    @staticmethod
    def _to_domain(row: GuardianChildLinkRow) -> GuardianChildLink:
        return GuardianChildLink(
            id=str(row.id), guardian_user_id=str(row.guardian_user_id),
            child_user_id=str(row.child_user_id), status=GuardianChildLinkStatus(row.status),
            child_consented_at=row.child_consented_at, safety_enabled_at=row.safety_enabled_at,
            created_at=row.created_at, updated_at=row.updated_at,
        )


class PostgresGuardChatStateRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, child_user_id: str, chat_id: str) -> GuardChatState | None:
        async with self._session_factory() as session:
            row = (await session.execute(select(GuardChatStateRow).where(
                GuardChatStateRow.child_user_id == child_user_id,
                GuardChatStateRow.chat_id == chat_id,
            ))).scalar_one_or_none()
            return self._to_domain(row) if row else None

    async def upsert_on_message(
        self, *, child_user_id: str, chat_id: str, observed_at: datetime,
        next_analysis_at: datetime, pending_since: datetime,
        next_analysis_reason: str | None = None, chat_name: str | None = None,
        is_group: bool = False,
    ) -> GuardChatState:
        async with self._session_factory() as session:
            stmt = (
                pg_insert(GuardChatStateRow)
                .values(
                    child_user_id=child_user_id, chat_id=chat_id, activity_version=1,
                    last_message_at=observed_at, last_analyzed_version=0,
                    last_decision=GuardDecision.NONE.value, pending_since=pending_since,
                    next_analysis_at=next_analysis_at, next_analysis_reason=next_analysis_reason,
                    chat_name=chat_name, is_group=is_group,
                )
                .on_conflict_do_update(
                    index_elements=["child_user_id", "chat_id"],
                    set_={
                        "activity_version": GuardChatStateRow.activity_version + 1,
                        "last_message_at": observed_at,
                        "pending_since": func.coalesce(GuardChatStateRow.pending_since, pending_since),
                        "next_analysis_at": func.least(GuardChatStateRow.next_analysis_at, next_analysis_at),
                        "next_analysis_reason": func.coalesce(
                            GuardChatStateRow.next_analysis_reason, next_analysis_reason
                        ),
                        "chat_name": chat_name, "is_group": is_group,
                        "updated_at": datetime.now(timezone.utc),
                    },
                    where=GuardChatStateRow.last_message_at <= observed_at,
                )
            )
            await session.execute(stmt)
            row = (await session.execute(select(GuardChatStateRow).where(
                GuardChatStateRow.child_user_id == child_user_id,
                GuardChatStateRow.chat_id == chat_id,
            ))).scalar_one()
            await session.commit()
            return self._to_domain(row)

    async def list_due(self, now: datetime, *, limit: int = 20) -> list[GuardChatState]:
        async with self._session_factory() as session:
            stmt = select(GuardChatStateRow).where(
                GuardChatStateRow.next_analysis_at.is_not(None),
                GuardChatStateRow.next_analysis_at <= now,
                GuardChatStateRow.activity_version > GuardChatStateRow.last_analyzed_version,
            ).order_by(GuardChatStateRow.next_analysis_at).limit(limit)
            return [self._to_domain(row) for row in (await session.execute(stmt)).scalars().all()]

    async def mark_processed(
        self, child_user_id: str, chat_id: str, target_version: int, *,
        decision: GuardDecision, analyzed_at: datetime,
    ) -> bool:
        async with self._session_factory() as session:
            stmt = update(GuardChatStateRow).where(
                GuardChatStateRow.child_user_id == child_user_id,
                GuardChatStateRow.chat_id == chat_id,
                GuardChatStateRow.activity_version == target_version,
            ).values(
                last_analyzed_version=target_version, last_decision=decision.value,
                last_analysis_at=analyzed_at, pending_since=None, next_analysis_at=None,
                next_analysis_reason=None, updated_at=analyzed_at,
            )
            result = cast(CursorResult[Any], await session.execute(stmt))
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    def _to_domain(row: GuardChatStateRow) -> GuardChatState:
        return GuardChatState(
            child_user_id=str(row.child_user_id), chat_id=row.chat_id,
            activity_version=row.activity_version, last_message_at=row.last_message_at,
            last_analyzed_version=row.last_analyzed_version,
            last_decision=GuardDecision(row.last_decision),
            last_analysis_at=row.last_analysis_at, pending_since=row.pending_since,
            next_analysis_at=row.next_analysis_at, next_analysis_reason=row.next_analysis_reason,
            chat_name=row.chat_name, is_group=row.is_group,
            created_at=row.created_at, updated_at=row.updated_at,
        )


class PostgresGuardAnalysisRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, record: GuardAnalysisRecord) -> str:
        async with self._session_factory() as session:
            values: dict[str, Any] = {
                "child_user_id": record.child_user_id,
                "connection_id": record.connection_id,
                "chat_id": record.chat_id,
                "target_version": record.target_version,
                "signals": list(record.signals),
                "categories": list(record.categories),
                "confidence": record.confidence,
                "reason": record.reason,
                "evidence_message_ids": list(record.evidence_message_ids),
                "decision": record.decision.value,
                "model": record.model,
                "prompt_version": record.prompt_version,
                "analyzer_version": record.analyzer_version,
                "taxonomy_version": record.taxonomy_version,
                "diagnostics": record.diagnostics,
                "schedule_reason": record.schedule_reason,
                "pending_since": record.pending_since,
                "scheduled_for": record.scheduled_for,
            }
            if record.id is not None:
                values["id"] = record.id
            stmt = pg_insert(GuardAnalysisResultRow).values(values).returning(
                GuardAnalysisResultRow.id
            )
            result_id = str((await session.execute(stmt)).scalar_one())
            await session.commit()
            return result_id

    async def list_for_children(
        self, *, child_user_ids: list[str], limit: int = 500
    ) -> list[GuardAnalysisRecord]:
        if not child_user_ids:
            return []
        async with self._session_factory() as session:
            stmt = select(GuardAnalysisResultRow).where(
                GuardAnalysisResultRow.child_user_id.in_(child_user_ids)
            ).order_by(GuardAnalysisResultRow.created_at.desc()).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_domain(row) for row in rows]

    async def list_for_replay(
        self, *, child_user_id: str, chat_id: str, since: datetime, before_version: int,
    ) -> list[GuardAnalysisRecord]:
        async with self._session_factory() as session:
            stmt = select(GuardAnalysisResultRow).where(
                GuardAnalysisResultRow.child_user_id == child_user_id,
                GuardAnalysisResultRow.chat_id == chat_id,
                GuardAnalysisResultRow.created_at >= since,
                GuardAnalysisResultRow.target_version < before_version,
            ).order_by(GuardAnalysisResultRow.target_version, GuardAnalysisResultRow.created_at)
            return [self._to_domain(row) for row in (await session.execute(stmt)).scalars().all()]

    @staticmethod
    def _to_domain(row: GuardAnalysisResultRow) -> GuardAnalysisRecord:
        return GuardAnalysisRecord(
            id=str(row.id), child_user_id=str(row.child_user_id), connection_id=str(row.connection_id),
            chat_id=row.chat_id, target_version=row.target_version,
            signals=tuple(row.signals), categories=tuple(row.categories), confidence=row.confidence,
            reason=row.reason, evidence_message_ids=tuple(row.evidence_message_ids),
            decision=GuardDecision(row.decision), model=row.model, prompt_version=row.prompt_version,
            analyzer_version=row.analyzer_version, taxonomy_version=row.taxonomy_version,
            created_at=row.created_at, diagnostics=row.diagnostics or {},
            schedule_reason=row.schedule_reason, pending_since=row.pending_since,
            scheduled_for=row.scheduled_for,
        )


class PostgresGuardAnalysisCommitRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def commit_if_current(
        self, *, child_user_id: str, chat_id: str, target_version: int,
        record: GuardAnalysisRecord,
    ) -> tuple[str, str | None]:
        async with self._session_factory() as session:
            try:
                row = (await session.execute(select(GuardChatStateRow).where(
                    GuardChatStateRow.child_user_id == child_user_id,
                    GuardChatStateRow.chat_id == chat_id,
                ).with_for_update())).scalar_one_or_none()
                if row is None:
                    await session.rollback()
                    return "missing", None
                if row.activity_version != target_version:
                    await session.rollback()
                    return "stale", None
                values: dict[str, Any] = {
                    "child_user_id": record.child_user_id,
                    "connection_id": record.connection_id,
                    "chat_id": record.chat_id,
                    "target_version": record.target_version,
                    "signals": list(record.signals),
                    "categories": list(record.categories),
                    "confidence": record.confidence,
                    "reason": record.reason,
                    "evidence_message_ids": list(record.evidence_message_ids),
                    "decision": record.decision.value,
                    "model": record.model,
                    "prompt_version": record.prompt_version,
                    "analyzer_version": record.analyzer_version,
                    "taxonomy_version": record.taxonomy_version,
                    "diagnostics": record.diagnostics,
                    "schedule_reason": record.schedule_reason,
                    "pending_since": record.pending_since,
                    "scheduled_for": record.scheduled_for,
                }
                if record.id is not None:
                    values["id"] = record.id
                result_stmt = pg_insert(GuardAnalysisResultRow).values(values).on_conflict_do_nothing(
                    constraint="guard_analysis_results_version_key"
                ).returning(GuardAnalysisResultRow.id)
                result_id = (await session.execute(result_stmt)).scalar_one_or_none()
                if result_id is None:
                    await session.rollback()
                    return "committed", None
                now = datetime.now(timezone.utc)
                await session.execute(update(GuardChatStateRow).where(
                    GuardChatStateRow.child_user_id == child_user_id,
                    GuardChatStateRow.chat_id == chat_id,
                    GuardChatStateRow.activity_version == target_version,
                ).values(
                    last_analyzed_version=target_version, last_decision=record.decision.value,
                    last_analysis_at=now, pending_since=None, next_analysis_at=None,
                    next_analysis_reason=None, updated_at=now,
                ))
                await session.commit()
                return "committed", str(result_id)
            except Exception:
                await session.rollback()
                raise
