"""PostgreSQL repository for the WAHA worker registry.

``register`` is an idempotent upsert on ``worker_id``: the DO UPDATE arm
refreshes reported state and Echo's ``last_seen_at`` while leaving
``first_registered_at`` untouched. ``RETURNING (xmax = 0)`` tells the caller
whether the row was newly inserted (first registration) or updated
(recurring heartbeat).

Availability is derived at read time from the server-side ``last_seen_at``
and reported health — never from the worker's own clock, so clock skew
cannot flip availability.

Phase 3 revision: ``management_protocol`` (backward compatible — absent/0
means a batch-only agent) gates onboarding eligibility, and the first
management-capable registration issues a per-worker management token whose
SHA-256 hash is stored (the plaintext is returned exactly once and never
persisted). ``verify_worker_token`` binds the management endpoints to a
single worker identity via a constant-time compare.
"""

from __future__ import annotations

import hmac
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.waha import (
    WahaRegistrationResult,
    WahaWorkerRegistration,
    WahaWorkerStatus,
    generate_worker_token,
    hash_worker_token_secret,
    parse_worker_token,
)

__all__ = ["PostgresWahaWorkerRegistry"]


_REGISTER_SQL = """
    INSERT INTO waha_workers (
        worker_id, region, endpoint, max_sessions, active_sessions,
        agent_version, health, reported_heartbeat, last_seen_at,
        first_registered_at, pending, management_protocol
    ) VALUES (
        :worker_id, :region, :endpoint, :max_sessions, :active_sessions,
        :agent_version, :health, :reported_heartbeat, now(), now(), :pending,
        :management_protocol
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
        pending = EXCLUDED.pending,
        management_protocol = EXCLUDED.management_protocol
    RETURNING (xmax = 0) AS first_registration, management_token_hash
"""


class PostgresWahaWorkerRegistry:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def register(
        self, registration: WahaWorkerRegistration
    ) -> WahaRegistrationResult:
        async with self._session_factory() as session:
            row = (
                await session.execute(
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
                        "management_protocol": registration.management_protocol,
                    },
                )
            ).mappings().one()
            first = bool(row["first_registration"])
            issued_token: str | None = None

            # Issue a per-worker management token on the first
            # management-capable registration (also covers batch-only
            # workers upgrading their agent: their re-registration with
            # management_protocol >= 1 has no token yet).
            if (
                registration.management_protocol >= 1
                and row["management_token_hash"] is None
            ):
                issued_token = generate_worker_token(registration.worker_id)
                parsed = parse_worker_token(issued_token)
                assert parsed is not None
                _worker_id, secret = parsed
                assert _worker_id == registration.worker_id
                await session.execute(
                    text(
                        "UPDATE waha_workers SET management_token_hash = :hash"
                        " WHERE worker_id = :worker_id"
                    ),
                    {
                        "hash": hash_worker_token_secret(secret),
                        "worker_id": registration.worker_id,
                    },
                )
            await session.commit()
            return WahaRegistrationResult(
                first=first, issued_worker_token=issued_token
            )

    async def verify_worker_token(
        self, authorization: str | None
    ) -> str | None:
        """Resolve a management bearer token to its worker id.

        The token embeds the worker id (direct row lookup) and a secret that
        is compared in constant time against the stored hash. Returns the
        worker id, or ``None`` for anything malformed, unknown or mismatched.
        """
        if not authorization or not authorization.startswith("Bearer "):
            return None
        parsed = parse_worker_token(authorization[len("Bearer "):])
        if parsed is None:
            return None
        worker_id, secret = parsed
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT management_token_hash FROM waha_workers"
                        " WHERE worker_id = :worker_id"
                    ),
                    {"worker_id": worker_id},
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        expected = hash_worker_token_secret(secret)
        if not hmac.compare_digest(bytes(row), expected):
            return None
        return worker_id

    async def list_workers(
        self, *, heartbeat_timeout_seconds: float = 90.0
    ) -> list[WahaWorkerStatus]:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT worker_id, region, endpoint, max_sessions,"
                            " active_sessions, agent_version, health,"
                            " reported_heartbeat, last_seen_at,"
                            " first_registered_at, pending, management_protocol"
                            " FROM waha_workers ORDER BY worker_id"
                        )
                    )
                )
                .mappings()
                .all()
            )
            return [
                WahaWorkerStatus(
                    worker_id=row["worker_id"],
                    region=row["region"],
                    endpoint=row["endpoint"],
                    max_sessions=row["max_sessions"],
                    active_sessions=row["active_sessions"],
                    agent_version=row["agent_version"],
                    health=row["health"],
                    reported_heartbeat=row["reported_heartbeat"],
                    last_seen_at=row["last_seen_at"],
                    first_registered_at=row["first_registered_at"],
                    pending=row["pending"],
                    available=(
                        (now - row["last_seen_at"]).total_seconds()
                        <= heartbeat_timeout_seconds
                        and row["health"] == "ok"
                    ),
                    has_capacity=row["active_sessions"] < row["max_sessions"],
                )
                for row in rows
            ]
