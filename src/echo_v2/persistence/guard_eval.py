"""PostgreSQL persistence for isolated Guard Live Compare runs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, cast, runtime_checkable

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.guard_eval import (
    GuardEvalAdmission,
    GuardEvalJudgment,
    GuardEvalJudgmentLabel,
    GuardEvalRun,
    GuardEvalRunBundle,
    GuardEvalRunStatus,
    GuardEvalSnapshot,
)
from echo_v2.persistence.orm import (
    GuardEvalJudgmentRow,
    GuardEvalRunRow,
    GuardEvalSnapshotRow,
)

__all__ = ["GuardEvalRepository", "PostgresGuardEvalRepository"]

_ADMISSION_LOCK_KEY = 0x4543484F5F455641


@runtime_checkable
class GuardEvalRepository(Protocol):
    async def admit_run(
        self,
        *,
        scenario_id: str,
        requester_key: str,
        request_id: str,
        gold_version: str,
        gold_label: str,
        model: str,
        prompt_version: str,
        analyzer_version: str,
        taxonomy_version: str,
        metadata: dict[str, Any],
        max_active: int,
        daily_limit: int,
        requester_limit: int,
        requester_window: timedelta,
        now: datetime | None = None,
    ) -> GuardEvalAdmission: ...

    async def recover_stale_runs(
        self, *, stale_after: timedelta, now: datetime | None = None
    ) -> int: ...

    async def save_snapshot(
        self,
        *,
        run_id: str,
        scenario_id: str,
        ordinal: int,
        after_message_id: str,
        expected: dict[str, Any],
        actual: dict[str, Any],
        diff: dict[str, Any],
        has_difference: bool,
        failure: bool,
    ) -> GuardEvalSnapshot: ...

    async def complete_run(self, run_id: str, *, now: datetime | None = None) -> GuardEvalRun | None: ...

    async def fail_run(
        self, run_id: str, *, error: str, now: datetime | None = None
    ) -> GuardEvalRun | None: ...

    async def get_run(self, run_id: str) -> GuardEvalRunBundle | None: ...

    async def list_runs(
        self, *, scenario_id: str | None = None, limit: int = 20
    ) -> tuple[GuardEvalRun, ...]: ...

    async def set_judgment(
        self,
        *,
        snapshot_id: str,
        reviewer_key: str,
        label: GuardEvalJudgmentLabel,
        note: str | None,
        reason: str | None,
    ) -> GuardEvalJudgment | None: ...


class PostgresGuardEvalRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def admit_run(
        self,
        *,
        scenario_id: str,
        requester_key: str,
        request_id: str,
        gold_version: str,
        gold_label: str,
        model: str,
        prompt_version: str,
        analyzer_version: str,
        taxonomy_version: str,
        metadata: dict[str, Any],
        max_active: int,
        daily_limit: int,
        requester_limit: int,
        requester_window: timedelta,
        now: datetime | None = None,
    ) -> GuardEvalAdmission:
        current = now or datetime.now(timezone.utc)
        day_start = current.astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        async with self._session_factory() as session, session.begin():
            await session.execute(select(func.pg_advisory_xact_lock(_ADMISSION_LOCK_KEY)))
            duplicate = (
                await session.execute(
                    select(GuardEvalRunRow).where(
                        GuardEvalRunRow.requester_key == requester_key,
                        GuardEvalRunRow.request_id == request_id,
                    )
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                return GuardEvalAdmission(
                    run=self._run(duplicate), allowed=True, duplicate=True
                )

            stale_before = current - requester_window * 4
            await session.execute(
                update(GuardEvalRunRow)
                .where(
                    GuardEvalRunRow.status == GuardEvalRunStatus.RUNNING.value,
                    GuardEvalRunRow.created_at < stale_before,
                )
                .values(
                    status=GuardEvalRunStatus.FAILED.value,
                    error="run recovered after executor timeout",
                    completed_at=current,
                )
            )
            active = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(GuardEvalRunRow)
                        .where(GuardEvalRunRow.status == GuardEvalRunStatus.RUNNING.value)
                    )
                ).scalar_one()
            )
            if active >= max_active:
                return GuardEvalAdmission(run=None, allowed=False, reason="active_limit")
            daily = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(GuardEvalRunRow)
                        .where(GuardEvalRunRow.created_at >= day_start)
                    )
                ).scalar_one()
            )
            if daily >= daily_limit:
                return GuardEvalAdmission(run=None, allowed=False, reason="daily_limit")
            requester_since = current - requester_window
            requester_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(GuardEvalRunRow)
                        .where(
                            GuardEvalRunRow.requester_key == requester_key,
                            GuardEvalRunRow.created_at >= requester_since,
                        )
                    )
                ).scalar_one()
            )
            if requester_count >= requester_limit:
                return GuardEvalAdmission(run=None, allowed=False, reason="requester_limit")
            row = (
                await session.execute(
                    pg_insert(GuardEvalRunRow)
                    .values(
                        scenario_id=scenario_id,
                        requester_key=requester_key,
                        request_id=request_id,
                        gold_version=gold_version,
                        gold_label=gold_label,
                        status=GuardEvalRunStatus.RUNNING.value,
                        model=model,
                        prompt_version=prompt_version,
                        analyzer_version=analyzer_version,
                        taxonomy_version=taxonomy_version,
                        metadata_json=metadata,
                        started_at=current,
                    )
                    .returning(GuardEvalRunRow)
                )
            ).scalar_one()
            return GuardEvalAdmission(run=self._run(row), allowed=True)

    async def recover_stale_runs(
        self, *, stale_after: timedelta, now: datetime | None = None
    ) -> int:
        current = now or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(GuardEvalRunRow)
                    .where(
                        GuardEvalRunRow.status == GuardEvalRunStatus.RUNNING.value,
                        GuardEvalRunRow.created_at < current - stale_after,
                    )
                    .values(
                        status=GuardEvalRunStatus.FAILED.value,
                        error="run recovered after executor timeout",
                        completed_at=current,
                    )
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def save_snapshot(
        self,
        *,
        run_id: str,
        scenario_id: str,
        ordinal: int,
        after_message_id: str,
        expected: dict[str, Any],
        actual: dict[str, Any],
        diff: dict[str, Any],
        has_difference: bool,
        failure: bool,
    ) -> GuardEvalSnapshot:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    pg_insert(GuardEvalSnapshotRow)
                    .values(
                        run_id=run_id,
                        scenario_id=scenario_id,
                        ordinal=ordinal,
                        after_message_id=after_message_id,
                        expected=expected,
                        actual=actual,
                        diff=diff,
                        has_difference=has_difference,
                        failure=failure,
                    )
                    .returning(GuardEvalSnapshotRow)
                )
            ).scalar_one()
            await session.commit()
            return self._snapshot(row)

    async def complete_run(self, run_id: str, *, now: datetime | None = None) -> GuardEvalRun | None:
        return await self._finish(run_id, GuardEvalRunStatus.COMPLETED, now=now)

    async def fail_run(
        self, run_id: str, *, error: str, now: datetime | None = None
    ) -> GuardEvalRun | None:
        return await self._finish(run_id, GuardEvalRunStatus.FAILED, error=error, now=now)

    async def _finish(
        self,
        run_id: str,
        status: GuardEvalRunStatus,
        *,
        error: str | None = None,
        now: datetime | None = None,
    ) -> GuardEvalRun | None:
        current = now or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    update(GuardEvalRunRow)
                    .where(
                        GuardEvalRunRow.id == run_id,
                        GuardEvalRunRow.status == GuardEvalRunStatus.RUNNING.value,
                    )
                    .values(status=status.value, error=error, completed_at=current)
                    .returning(GuardEvalRunRow)
                )
            ).scalar_one_or_none()
            await session.commit()
            return self._run(row) if row else None

    async def get_run(self, run_id: str) -> GuardEvalRunBundle | None:
        async with self._session_factory() as session:
            run = (
                await session.execute(
                    select(GuardEvalRunRow).where(GuardEvalRunRow.id == run_id)
                )
            ).scalar_one_or_none()
            if run is None:
                return None
            snapshots = (
                await session.execute(
                    select(GuardEvalSnapshotRow)
                    .where(GuardEvalSnapshotRow.run_id == run_id)
                    .order_by(GuardEvalSnapshotRow.ordinal)
                )
            ).scalars().all()
            snapshot_ids = [str(snapshot.id) for snapshot in snapshots]
            judgments = (
                await session.execute(
                    select(GuardEvalJudgmentRow)
                    .where(GuardEvalJudgmentRow.snapshot_id.in_(snapshot_ids))
                )
            ).scalars().all() if snapshot_ids else []
            return GuardEvalRunBundle(
                run=self._run(run),
                snapshots=tuple(self._snapshot(row) for row in snapshots),
                judgments=tuple(self._judgment(row) for row in judgments),
            )

    async def list_runs(
        self, *, scenario_id: str | None = None, limit: int = 20
    ) -> tuple[GuardEvalRun, ...]:
        async with self._session_factory() as session:
            statement = select(GuardEvalRunRow).order_by(GuardEvalRunRow.created_at.desc()).limit(limit)
            if scenario_id is not None:
                statement = statement.where(GuardEvalRunRow.scenario_id == scenario_id)
            rows = (await session.execute(statement)).scalars().all()
            return tuple(self._run(row) for row in rows)

    async def set_judgment(
        self,
        *,
        snapshot_id: str,
        reviewer_key: str,
        label: GuardEvalJudgmentLabel,
        note: str | None,
        reason: str | None,
    ) -> GuardEvalJudgment | None:
        async with self._session_factory() as session:
            if (
                await session.execute(
                    select(GuardEvalSnapshotRow.id).where(GuardEvalSnapshotRow.id == snapshot_id)
                )
            ).scalar_one_or_none() is None:
                return None
            now = datetime.now(timezone.utc)
            row = (
                await session.execute(
                    pg_insert(GuardEvalJudgmentRow)
                    .values(
                        snapshot_id=snapshot_id,
                        reviewer_key=reviewer_key,
                        label=label.value,
                        note=note,
                        reason=reason,
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        constraint="guard_eval_judgments_snapshot_reviewer_key",
                        set_={
                            "label": label.value,
                            "note": note,
                            "reason": reason,
                            "updated_at": now,
                        },
                    )
                    .returning(GuardEvalJudgmentRow)
                )
            ).scalar_one()
            await session.commit()
            return self._judgment(row)

    @staticmethod
    def _run(row: GuardEvalRunRow) -> GuardEvalRun:
        return GuardEvalRun(
            id=str(row.id),
            scenario_id=row.scenario_id,
            requester_key=row.requester_key,
            request_id=row.request_id,
            gold_version=row.gold_version,
            gold_label=row.gold_label,
            status=GuardEvalRunStatus(row.status),
            model=row.model,
            prompt_version=row.prompt_version,
            analyzer_version=row.analyzer_version,
            taxonomy_version=row.taxonomy_version,
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
            error=row.error,
            metadata=row.metadata_json,
        )

    @staticmethod
    def _snapshot(row: GuardEvalSnapshotRow) -> GuardEvalSnapshot:
        return GuardEvalSnapshot(
            id=str(row.id),
            run_id=str(row.run_id),
            scenario_id=row.scenario_id,
            ordinal=row.ordinal,
            after_message_id=row.after_message_id,
            expected=row.expected,
            actual=row.actual,
            diff=row.diff,
            has_difference=row.has_difference,
            failure=row.failure,
            created_at=row.created_at,
        )

    @staticmethod
    def _judgment(row: GuardEvalJudgmentRow) -> GuardEvalJudgment:
        return GuardEvalJudgment(
            snapshot_id=str(row.snapshot_id),
            reviewer_key=row.reviewer_key,
            label=GuardEvalJudgmentLabel(row.label),
            note=row.note,
            reason=row.reason,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
