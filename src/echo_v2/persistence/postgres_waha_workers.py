"""PostgreSQL repository for the WAHA worker registry.

``register`` is an idempotent upsert on ``worker_id``: the DO UPDATE arm
refreshes reported state and Echo's ``last_seen_at`` while leaving
``first_registered_at`` untouched. ``RETURNING (xmax = 0)`` tells the caller
whether the row was newly inserted (first registration) or updated
(recurring heartbeat).

Availability is derived at read time from the server-side ``last_seen_at``
and reported health — never from the worker's own clock, so clock skew
cannot flip availability.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.waha import WahaWorkerRegistration, WahaWorkerStatus
from echo_v2.persistence.orm import WahaWorkerRow

__all__ = ["PostgresWahaWorkerRegistry"]

_REGISTER_SQL = """
    INSERT INTO waha_workers (
        worker_id, region, endpoint, max_sessions, active_sessions,
        agent_version, health, reported_heartbeat, last_seen_at,
        first_registered_at, pending
    ) VALUES (
        :worker_id, :region, :endpoint, :max_sessions, :active_sessions,
        :agent_version, :health, :reported_heartbeat, now(), now(), :pending
    )
    ON CONFLICT (worker_id) DO UPDATE SET
        region = EXCLUDED.region,
        endpoint = EXCLUDED.endpoint,
        max_sessions = EXCLUDED.max_sessions,
        active_sessions = EXCLUDED.active_sessions,
        agent_version = EXCLUDED.agent_version,
        health = EXCLUDED.health,
        reported_heartbeat = EXCLUDED.reported_heartbeat,
        last_seen_at = now(),
        pending = EXCLUDED.pending
    RETURNING (xmax = 0) AS first_registration
"""


class PostgresWahaWorkerRegistry:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def register(self, registration: WahaWorkerRegistration) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                text(_REGISTER_SQL),
                {
                    "worker_id": registration.worker_id,
                    "region": registration.region,
                    "endpoint": registration.endpoint,
                    "max_sessions": registration.max_sessions,
                    "active_sessions": registration.active_sessions,
                    "agent_version": registration.version,
                    "health": registration.health,
                    "reported_heartbeat": registration.reported_heartbeat,
                    "pending": registration.pending,
                },
            )
            first = result.scalar_one()
            await session.commit()
            return bool(first)

    async def list_workers(
        self, *, heartbeat_timeout_seconds: float = 90.0
    ) -> list[WahaWorkerStatus]:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(WahaWorkerRow).order_by(WahaWorkerRow.worker_id)
                    )
                )
                .scalars()
                .all()
            )
            return [
                WahaWorkerStatus(
                    worker_id=row.worker_id,
                    region=row.region,
                    endpoint=row.endpoint,
                    max_sessions=row.max_sessions,
                    active_sessions=row.active_sessions,
                    agent_version=row.agent_version,
                    health=row.health,
                    reported_heartbeat=row.reported_heartbeat,
                    last_seen_at=row.last_seen_at,
                    first_registered_at=row.first_registered_at,
                    pending=row.pending,
                    available=(
                        (now - row.last_seen_at).total_seconds()
                        <= heartbeat_timeout_seconds
                        and row.health == "ok"
                    ),
                    has_capacity=row.active_sessions < row.max_sessions,
                )
                for row in rows
            ]
