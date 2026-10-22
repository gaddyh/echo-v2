"""Persistence contracts for alerted incidents.

Protocol + InMemory + Postgres implementations.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.alerted_incident import AlertedIncident
from echo_v2.persistence.orm import AlertedIncidentRow

__all__ = [
    "AlertedIncidentRepository",
    "InMemoryAlertedIncidentRepository",
    "PostgresAlertedIncidentRepository",
]


class AlertedIncidentRepository(Protocol):
    async def create(self, incident: AlertedIncident) -> AlertedIncident: ...

    async def get(self, incident_id: str) -> AlertedIncident | None: ...

    async def list_active(
        self, child_user_id: str, chat_id: str, alert_type: str
    ) -> list[AlertedIncident]: ...

    async def list_recently_closed(
        self,
        child_user_id: str,
        chat_id: str,
        alert_type: str,
        since: datetime,
    ) -> list[AlertedIncident]: ...

    async def list_for_children(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime,
        created_before: datetime,
        chat_id: str | None = None,
        alert_type: str | None = None,
        limit: int = 200,
    ) -> list[AlertedIncident]: ...

    async def update(self, incident: AlertedIncident) -> None: ...

    async def close(self, incident_id: str, closed_at: datetime) -> None: ...

    async def find_by_assessment_id(
        self,
        assessment_id: str,
        child_user_id: str,
        chat_id: str,
        alert_type: str,
    ) -> AlertedIncident | None: ...

    def acquire_lock(
        self, child_user_id: str, chat_id: str, alert_type: str
    ) -> AbstractAsyncContextManager[None]: ...


class InMemoryAlertedIncidentRepository:
    def __init__(self) -> None:
        self._incidents: dict[str, AlertedIncident] = {}

    async def create(self, incident: AlertedIncident) -> AlertedIncident:
        self._incidents[incident.id] = incident
        return incident

    async def get(self, incident_id: str) -> AlertedIncident | None:
        return self._incidents.get(incident_id)

    async def list_active(
        self, child_user_id: str, chat_id: str, alert_type: str
    ) -> list[AlertedIncident]:
        return [
            inc
            for inc in self._incidents.values()
            if inc.child_user_id == child_user_id
            and inc.chat_id == chat_id
            and inc.alert_type == alert_type
            and inc.closed_at is None
        ]

    async def list_recently_closed(
        self,
        child_user_id: str,
        chat_id: str,
        alert_type: str,
        since: datetime,
    ) -> list[AlertedIncident]:
        return [
            inc
            for inc in self._incidents.values()
            if inc.child_user_id == child_user_id
            and inc.chat_id == chat_id
            and inc.alert_type == alert_type
            and inc.closed_at is not None
            and inc.closed_at >= since
        ]

    async def list_for_children(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime,
        created_before: datetime,
        chat_id: str | None = None,
        alert_type: str | None = None,
        limit: int = 200,
    ) -> list[AlertedIncident]:
        results = [
            inc
            for inc in self._incidents.values()
            if inc.child_user_id in child_user_ids
            and inc.created_at >= created_after
            and inc.created_at <= created_before
            and (chat_id is None or inc.chat_id == chat_id)
            and (alert_type is None or inc.alert_type == alert_type)
        ]
        results.sort(key=lambda inc: inc.created_at, reverse=True)
        return results[:limit]

    async def update(self, incident: AlertedIncident) -> None:
        self._incidents[incident.id] = incident

    async def close(self, incident_id: str, closed_at: datetime) -> None:
        inc = self._incidents.get(incident_id)
        if inc is not None:
            self._incidents[incident_id] = replace(inc, closed_at=closed_at)

    async def find_by_assessment_id(
        self,
        assessment_id: str,
        child_user_id: str,
        chat_id: str,
        alert_type: str,
    ) -> AlertedIncident | None:
        for inc in self._incidents.values():
            if (
                inc.child_user_id == child_user_id
                and inc.chat_id == chat_id
                and inc.alert_type == alert_type
                and assessment_id in inc.assessment_ids
            ):
                return inc
        return None

    @asynccontextmanager
    async def acquire_lock(
        self, child_user_id: str, chat_id: str, alert_type: str
    ) -> AsyncIterator[None]:
        yield


class PostgresAlertedIncidentRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._session = session

    def _get_session(self) -> AsyncSession:
        if self._session is not None:
            return self._session
        return self._session_factory()

    async def create(self, incident: AlertedIncident) -> AlertedIncident:
        values = _to_row_dict(incident)
        session = self._get_session()
        own_session = self._session is None
        try:
            await session.execute(
                pg_insert(AlertedIncidentRow).values(values)
            )
            if own_session:
                await session.commit()
        except Exception:  # pragma: no cover - defensive rollback
            if own_session:
                await session.rollback()
            raise
        finally:
            if own_session:
                await session.close()
        return incident

    async def get(self, incident_id: str) -> AlertedIncident | None:
        session = self._get_session()
        own_session = self._session is None
        try:
            row = (
                await session.execute(
                    select(AlertedIncidentRow).where(
                        AlertedIncidentRow.id == incident_id
                    )
                )
            ).scalar_one_or_none()
            return _to_domain(row) if row is not None else None
        finally:
            if own_session:
                await session.close()

    async def list_active(
        self, child_user_id: str, chat_id: str, alert_type: str
    ) -> list[AlertedIncident]:
        session = self._get_session()
        own_session = self._session is None
        try:
            rows = (
                await session.execute(
                    select(AlertedIncidentRow).where(
                        AlertedIncidentRow.child_user_id == child_user_id,
                        AlertedIncidentRow.chat_id == chat_id,
                        AlertedIncidentRow.alert_type == alert_type,
                        AlertedIncidentRow.closed_at.is_(None),
                    )
                )
            ).scalars().all()
            return [_to_domain(row) for row in rows]
        finally:
            if own_session:
                await session.close()

    async def list_recently_closed(
        self,
        child_user_id: str,
        chat_id: str,
        alert_type: str,
        since: datetime,
    ) -> list[AlertedIncident]:
        session = self._get_session()
        own_session = self._session is None
        try:
            rows = (
                await session.execute(
                    select(AlertedIncidentRow).where(
                        AlertedIncidentRow.child_user_id == child_user_id,
                        AlertedIncidentRow.chat_id == chat_id,
                        AlertedIncidentRow.alert_type == alert_type,
                        AlertedIncidentRow.closed_at.is_not(None),
                        AlertedIncidentRow.closed_at >= since,
                    )
                )
            ).scalars().all()
            return [_to_domain(row) for row in rows]
        finally:
            if own_session:
                await session.close()

    async def list_for_children(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime,
        created_before: datetime,
        chat_id: str | None = None,
        alert_type: str | None = None,
        limit: int = 200,
    ) -> list[AlertedIncident]:
        session = self._get_session()
        own_session = self._session is None
        try:
            stmt = select(AlertedIncidentRow).where(
                AlertedIncidentRow.child_user_id.in_(child_user_ids),
                AlertedIncidentRow.created_at >= created_after,
                AlertedIncidentRow.created_at <= created_before,
            )
            if chat_id is not None:
                stmt = stmt.where(AlertedIncidentRow.chat_id == chat_id)
            if alert_type is not None:
                stmt = stmt.where(AlertedIncidentRow.alert_type == alert_type)
            stmt = stmt.order_by(AlertedIncidentRow.created_at.desc()).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(row) for row in rows]
        finally:
            if own_session:
                await session.close()

    async def update(self, incident: AlertedIncident) -> None:
        values = _to_row_dict(incident)
        values["updated_at"] = datetime.now(timezone.utc)
        session = self._get_session()
        own_session = self._session is None
        try:
            await session.execute(
                update(AlertedIncidentRow)
                .where(AlertedIncidentRow.id == incident.id)
                .values(values)
            )
            if own_session:
                await session.commit()
        except Exception:  # pragma: no cover - defensive rollback
            if own_session:
                await session.rollback()
            raise
        finally:
            if own_session:
                await session.close()

    async def close(self, incident_id: str, closed_at: datetime) -> None:
        session = self._get_session()
        own_session = self._session is None
        try:
            await session.execute(
                update(AlertedIncidentRow)
                .where(AlertedIncidentRow.id == incident_id)
                .values(closed_at=closed_at, updated_at=datetime.now(timezone.utc))
            )
            if own_session:
                await session.commit()
        except Exception:  # pragma: no cover - defensive rollback
            if own_session:
                await session.rollback()
            raise
        finally:
            if own_session:
                await session.close()

    async def find_by_assessment_id(
        self,
        assessment_id: str,
        child_user_id: str,
        chat_id: str,
        alert_type: str,
    ) -> AlertedIncident | None:
        session = self._get_session()
        own_session = self._session is None
        try:
            rows = (
                await session.execute(
                    select(AlertedIncidentRow).where(
                        AlertedIncidentRow.child_user_id == child_user_id,
                        AlertedIncidentRow.chat_id == chat_id,
                        AlertedIncidentRow.alert_type == alert_type,
                    )
                )
            ).scalars().all()
            for row in rows:
                if assessment_id in (row.assessment_ids or []):
                    return _to_domain(row)
            return None
        finally:
            if own_session:
                await session.close()

    @asynccontextmanager
    async def acquire_lock(
        self, child_user_id: str, chat_id: str, alert_type: str
    ) -> AsyncIterator[None]:
        # Postgres advisory lock keyed on (child, chat, alert_type).
        # Uses pg_advisory_xact_lock for transaction-scoped locking.
        import hashlib

        from sqlalchemy import text

        session = self._get_session()
        own_session = self._session is None
        if own_session:
            session = self._session_factory()
        try:
            key = int(
                hashlib.sha256(
                    f"{child_user_id}:{chat_id}:{alert_type}".encode()
                ).hexdigest()[:15],
                16,
            )
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            yield
            if own_session:
                await session.commit()
        except Exception:  # pragma: no cover - defensive rollback
            if own_session:
                await session.rollback()
            raise
        finally:
            if own_session:
                await session.close()


def _to_row_dict(incident: AlertedIncident) -> dict[str, Any]:
    return {
        "id": incident.id,
        "child_user_id": incident.child_user_id,
        "chat_id": incident.chat_id,
        "alert_type": incident.alert_type,
        "summary": incident.summary,
        "severity_summary": incident.severity_summary,
        "notified_severity_summary": incident.notified_severity_summary,
        "evidence_message_ids": list(incident.evidence_message_ids),
        "assessment_ids": list(incident.assessment_ids),
        "last_seen_at": incident.last_seen_at,
        "closed_at": incident.closed_at,
        "related_incident_id": incident.related_incident_id,
        "notification_version": incident.notification_version,
        "created_at": incident.created_at,
        "updated_at": incident.updated_at,
    }


def _to_domain(row: AlertedIncidentRow) -> AlertedIncident:
    return AlertedIncident(
        id=row.id,
        child_user_id=row.child_user_id,
        chat_id=row.chat_id,
        alert_type=row.alert_type,
        summary=row.summary,
        severity_summary=row.severity_summary,
        notified_severity_summary=row.notified_severity_summary,
        evidence_message_ids=tuple(row.evidence_message_ids or []),
        assessment_ids=tuple(row.assessment_ids or []),
        last_seen_at=row.last_seen_at,
        closed_at=row.closed_at,
        related_incident_id=row.related_incident_id,
        notification_version=row.notification_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
