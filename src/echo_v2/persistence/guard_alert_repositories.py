"""Persistence implementations for Guard alert orchestration."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import TracebackType

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.guard_alert import (
    GuardAlert,
    GuardAlertEvaluation,
    GuardAlertEvaluationOutcome,
    GuardAlertKind,
    GuardAlertStatus,
    GuardThresholdState,
    GuardThresholdStateStatus,
)
from echo_v2.persistence.guard_repositories import GuardAlertRepository
from echo_v2.persistence.orm import (
    GuardAlertEvaluationRow,
    GuardAlertRow,
    GuardThresholdStateRow,
)

__all__ = ["InMemoryGuardAlertRepository", "PostgresGuardAlertRepository"]


class PostgresGuardAlertRepository(GuardAlertRepository):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        session: AsyncSession | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._shared_session = session

    def _session(self) -> _SessionContext:
        if self._shared_session is not None:
            return _SessionContext(self._shared_session, owns=False)
        return _SessionContext(self._session_factory(), owns=True)

    async def get_threshold_state(
        self, *, child_user_id: str, chat_id: str, threshold_family: str
    ) -> GuardThresholdState:
        async with self._session() as session:
            stmt = (
                pg_insert(GuardThresholdStateRow)
                .values(
                    child_user_id=child_user_id,
                    chat_id=chat_id,
                    threshold_family=threshold_family,
                    status=GuardThresholdStateStatus.INACTIVE.value,
                    last_processed_version=0,
                    non_actionable_count=0,
                )
                .on_conflict_do_nothing(
                    index_elements=["child_user_id", "chat_id", "threshold_family"]
                )
            )
            await session.execute(stmt)
            row = (
                await session.execute(
                    select(GuardThresholdStateRow)
                    .where(
                        GuardThresholdStateRow.child_user_id == child_user_id,
                        GuardThresholdStateRow.chat_id == chat_id,
                        GuardThresholdStateRow.threshold_family == threshold_family,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            return _state(row)

    async def list_threshold_states(
        self, *, child_user_id: str, chat_id: str
    ) -> list[GuardThresholdState]:
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(GuardThresholdStateRow).where(
                        GuardThresholdStateRow.child_user_id == child_user_id,
                        GuardThresholdStateRow.chat_id == chat_id,
                    )
                )
            ).scalars().all()
            return [_state(row) for row in rows]

    async def save_threshold_state(self, state: GuardThresholdState) -> None:
        async with self._session() as session:
            await session.execute(
                pg_insert(GuardThresholdStateRow)
                .values(
                    child_user_id=state.child_user_id,
                    chat_id=state.chat_id,
                    threshold_family=state.threshold_family,
                    status=state.status.value,
                    episode_start_version=state.episode_start_version,
                    current_kind=state.current_kind.value if state.current_kind else None,
                    last_processed_version=state.last_processed_version,
                    last_supporting_version=state.last_supporting_version,
                    last_supporting_at=state.last_supporting_at,
                    non_actionable_count=state.non_actionable_count,
                    non_actionable_since=state.non_actionable_since,
                    updated_at=state.updated_at or datetime.now(timezone.utc),
                )
                .on_conflict_do_update(
                    index_elements=["child_user_id", "chat_id", "threshold_family"],
                    set_={
                        "status": state.status.value,
                        "episode_start_version": state.episode_start_version,
                        "current_kind": state.current_kind.value if state.current_kind else None,
                        "last_processed_version": state.last_processed_version,
                        "last_supporting_version": state.last_supporting_version,
                        "last_supporting_at": state.last_supporting_at,
                        "non_actionable_count": state.non_actionable_count,
                        "non_actionable_since": state.non_actionable_since,
                        "updated_at": state.updated_at or datetime.now(timezone.utc),
                    },
                )
            )

    async def get_evaluation(
        self, analysis_result_id: str, guardian_user_id: str
    ) -> GuardAlertEvaluation | None:
        async with self._session() as session:
            row = (
                await session.execute(
                    select(GuardAlertEvaluationRow).where(
                        GuardAlertEvaluationRow.analysis_result_id == analysis_result_id,
                        GuardAlertEvaluationRow.guardian_user_id == guardian_user_id,
                    )
                )
            ).scalar_one_or_none()
            return _evaluation(row) if row else None

    async def save_evaluation(self, evaluation: GuardAlertEvaluation) -> bool:
        async with self._session() as session:
            stmt = (
                pg_insert(GuardAlertEvaluationRow)
                .values(
                    analysis_result_id=evaluation.analysis_result_id,
                    guardian_user_id=evaluation.guardian_user_id,
                    child_user_id=evaluation.child_user_id,
                    chat_id=evaluation.chat_id,
                    target_version=evaluation.target_version,
                    policy_version=evaluation.policy_version,
                    outcome=evaluation.outcome.value,
                    evaluated_at=evaluation.evaluated_at,
                    alert_id=evaluation.alert_id,
                )
                .on_conflict_do_nothing(
                    index_elements=["analysis_result_id", "guardian_user_id"]
                )
            )
            result = await session.execute(stmt)
            return bool(getattr(result, "rowcount", 0))

    async def get_by_event_key(self, logical_event_key: str) -> GuardAlert | None:
        async with self._session() as session:
            row = (
                await session.execute(
                    select(GuardAlertRow).where(
                        GuardAlertRow.logical_event_key == logical_event_key
                    )
                )
            ).scalar_one_or_none()
            return _alert(row) if row else None

    async def update_alert_status(self, alert_id: str, status: str) -> None:
        async with self._session() as session:
            await session.execute(
                update(GuardAlertRow)
                .where(GuardAlertRow.id == alert_id)
                .values(status=status)
            )

    async def get_by_session_id(
        self, *, session_id: str, guardian_user_id: str
    ) -> GuardAlert | None:
        async with self._session() as session:
            row = (
                await session.execute(
                    select(GuardAlertRow)
                    .where(
                        GuardAlertRow.guardian_user_id == guardian_user_id,
                        GuardAlertRow.metadata_json["session_id"].astext == session_id,
                    )
                    .order_by(GuardAlertRow.created_at.desc())
                )
            ).scalars().first()
            return _alert(row) if row else None

    async def list_evaluations_for_chat(
        self, *, child_user_id: str, chat_id: str, limit: int = 500
    ) -> list[GuardAlertEvaluation]:
        async with self._session() as session:
            stmt = select(GuardAlertEvaluationRow).where(
                GuardAlertEvaluationRow.child_user_id == child_user_id,
                GuardAlertEvaluationRow.chat_id == chat_id,
            ).order_by(GuardAlertEvaluationRow.evaluated_at).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [_evaluation(row) for row in rows]

    async def list_alerts_for_chat(
        self, *, child_user_id: str, chat_id: str, limit: int = 100
    ) -> list[GuardAlert]:
        async with self._session() as session:
            stmt = select(GuardAlertRow).where(
                GuardAlertRow.child_user_id == child_user_id,
                GuardAlertRow.chat_id == chat_id,
            ).order_by(GuardAlertRow.created_at).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [_alert(row) for row in rows]

    async def save_alert(self, alert: GuardAlert) -> bool:
        async with self._session() as session:
            stmt = (
                pg_insert(GuardAlertRow)
                .values(
                    id=alert.id,
                    analysis_result_id=alert.analysis_result_id,
                    guardian_user_id=alert.guardian_user_id,
                    child_user_id=alert.child_user_id,
                    chat_id=alert.chat_id,
                    kind=alert.kind.value,
                    categories=list(alert.categories),
                    summary=alert.summary,
                    policy_reason=alert.policy_reason,
                    policy_version=alert.policy_version,
                    status=alert.status.value,
                    scheduled_action_id=alert.scheduled_action_id,
                    logical_event_key=alert.logical_event_key,
                    threshold_family=alert.threshold_family,
                    episode_start_version=alert.episode_start_version,
                    detected_at=alert.detected_at,
                    expires_at=alert.expires_at,
                    created_at=alert.created_at,
                    metadata_json=alert.metadata,
                )
                .on_conflict_do_nothing(index_elements=["logical_event_key"])
            )
            result = await session.execute(stmt)
            return bool(getattr(result, "rowcount", 0))


class InMemoryGuardAlertRepository:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str, str], GuardThresholdState] = {}
        self.evaluations: dict[tuple[str, str], GuardAlertEvaluation] = {}
        self.alerts: dict[str, GuardAlert] = {}

    async def get_threshold_state(
        self, *, child_user_id: str, chat_id: str, threshold_family: str
    ) -> GuardThresholdState:
        key = (child_user_id, chat_id, threshold_family)
        state = self.states.get(key)
        if state is None:
            state = GuardThresholdState(
                child_user_id=child_user_id,
                chat_id=chat_id,
                threshold_family=threshold_family,
            )
            self.states[key] = state
        return state

    async def list_threshold_states(
        self, *, child_user_id: str, chat_id: str
    ) -> list[GuardThresholdState]:
        return [
            state for state in self.states.values()
            if state.child_user_id == child_user_id and state.chat_id == chat_id
        ]

    async def save_threshold_state(self, state: GuardThresholdState) -> None:
        self.states[(state.child_user_id, state.chat_id, state.threshold_family)] = state

    async def get_evaluation(
        self, analysis_result_id: str, guardian_user_id: str
    ) -> GuardAlertEvaluation | None:
        return self.evaluations.get((analysis_result_id, guardian_user_id))

    async def save_evaluation(self, evaluation: GuardAlertEvaluation) -> bool:
        key = (evaluation.analysis_result_id, evaluation.guardian_user_id)
        if key in self.evaluations:
            return False
        self.evaluations[key] = evaluation
        return True

    async def get_by_event_key(self, logical_event_key: str) -> GuardAlert | None:
        return next(
            (alert for alert in self.alerts.values() if alert.logical_event_key == logical_event_key),
            None,
        )

    async def get_by_session_id(
        self, *, session_id: str, guardian_user_id: str
    ) -> GuardAlert | None:
        rows = [
            alert
            for alert in self.alerts.values()
            if alert.guardian_user_id == guardian_user_id
            and alert.metadata
            and alert.metadata.get("session_id") == session_id
        ]
        return max(rows, key=lambda alert: alert.created_at, default=None)

    async def save_alert(self, alert: GuardAlert) -> bool:
        if await self.get_by_event_key(alert.logical_event_key) is not None:
            return False
        self.alerts[alert.id] = alert
        return True

    async def update_alert_status(self, alert_id: str, status: str) -> None:
        alert = self.alerts.get(alert_id)
        if alert is not None:
            self.alerts[alert_id] = replace(alert, status=GuardAlertStatus(status))

    async def list_evaluations_for_chat(
        self, *, child_user_id: str, chat_id: str, limit: int = 500
    ) -> list[GuardAlertEvaluation]:
        rows = [
            evaluation for evaluation in self.evaluations.values()
            if evaluation.child_user_id == child_user_id and evaluation.chat_id == chat_id
        ]
        rows.sort(key=lambda item: item.evaluated_at)
        return rows[-limit:]

    async def list_alerts_for_chat(
        self, *, child_user_id: str, chat_id: str, limit: int = 100
    ) -> list[GuardAlert]:
        rows = [
            alert for alert in self.alerts.values()
            if alert.child_user_id == child_user_id and alert.chat_id == chat_id
        ]
        rows.sort(key=lambda item: item.created_at)
        return rows[-limit:]


class _SessionContext:
    def __init__(self, session: AsyncSession, *, owns: bool) -> None:
        self._session = session
        self._owns = owns

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
            await self._session.close()


def _state(row: GuardThresholdStateRow) -> GuardThresholdState:
    return GuardThresholdState(
        child_user_id=str(row.child_user_id),
        chat_id=row.chat_id,
        threshold_family=row.threshold_family,
        status=GuardThresholdStateStatus(row.status),
        episode_start_version=row.episode_start_version,
        current_kind=GuardAlertKind(row.current_kind) if row.current_kind else None,
        last_processed_version=row.last_processed_version,
        last_supporting_version=row.last_supporting_version,
        last_supporting_at=row.last_supporting_at,
        non_actionable_count=row.non_actionable_count,
        non_actionable_since=row.non_actionable_since,
        updated_at=row.updated_at,
    )


def _evaluation(row: GuardAlertEvaluationRow) -> GuardAlertEvaluation:
    return GuardAlertEvaluation(
        analysis_result_id=str(row.analysis_result_id),
        guardian_user_id=str(row.guardian_user_id),
        child_user_id=str(row.child_user_id),
        chat_id=row.chat_id,
        target_version=row.target_version,
        policy_version=row.policy_version,
        outcome=GuardAlertEvaluationOutcome(row.outcome),
        evaluated_at=row.evaluated_at,
        alert_id=str(row.alert_id) if row.alert_id else None,
    )


def _alert(row: GuardAlertRow) -> GuardAlert:
    return GuardAlert(
        id=str(row.id),
        analysis_result_id=str(row.analysis_result_id),
        guardian_user_id=str(row.guardian_user_id),
        child_user_id=str(row.child_user_id),
        chat_id=row.chat_id,
        kind=GuardAlertKind(row.kind),
        categories=tuple(row.categories or ()),
        summary=row.summary,
        policy_reason=row.policy_reason,
        policy_version=row.policy_version,
        status=GuardAlertStatus(row.status),
        scheduled_action_id=str(row.scheduled_action_id),
        logical_event_key=row.logical_event_key,
        threshold_family=row.threshold_family,
        episode_start_version=row.episode_start_version,
        detected_at=row.detected_at,
        expires_at=row.expires_at,
        created_at=row.created_at,
        metadata=row.metadata_json or {},
    )
