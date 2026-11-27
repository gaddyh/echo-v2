"""PostgreSQL repository for WAHA session assignments + the agent command
queue (Phase 3, agent-based management).

Echo is the control plane: it decides placement, persists assignments and
enqueues management commands; the per-VM agent claims commands by polling
and executes them against its local WAHA (127.0.0.1:3000). Echo never
connects to a worker, and no DB transaction is ever held across agent
execution.

Allocation (``allocate_and_enqueue_create``) is atomic: advisory lock →
get-or-create the user's **stable** assignment (session name generated only
at initial allocation) → enqueue the ``create_and_start`` command if none is
active — one transaction, so an assignment can never exist without an
executable provisioning command. Retries reuse the same worker + session.

``apply_lifecycle`` row-locks the session row and applies an event-timestamp
fence: older events are ignored, identical duplicates do not re-apply, and
newer/changed events bump ``event_seq`` and (re)queue the transition's
``pending_effect``. Applied transitions are mirrored onto
``whatsapp_connections`` whenever that row exists (also covering callbacks
that arrived before it was created).

The command queue (``waha_commands``) is deliberately specific to WAHA
management — no generic task framework: claims are lease-bounded
(``FOR UPDATE SKIP LOCKED``, only the assigned worker's rows), expired
leases are reclaimable, attempts are capped (``failed``), and completion is
idempotent and worker-guarded.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.waha import (
    WahaAllocationError,
    WahaCommand,
    WahaSessionAssignment,
    WahaTransitionOutcome,
    effect_for_waha_status,
)

__all__ = ["PostgresWahaSessionRepository"]

# Transaction-scoped lock serializing fresh session allocations.
_ALLOCATION_LOCK_KEY = 8300065

_COLUMNS = (
    "session_name, user_id, worker_id, status, raw_status, last_status_at, "
    "event_seq, pending_effect, pending_effect_at, last_qr_hash, "
    "last_qr_image, qr_updated_at, account_phone, account_identity_at"
)

_SELECT_BY_SESSION = (
    f"SELECT {_COLUMNS} FROM waha_sessions WHERE session_name = :session_name"
)
_SELECT_BY_USER = (
    f"SELECT {_COLUMNS} FROM waha_sessions WHERE user_id = :user_id"
)

_COMMAND_COLUMNS = (
    "command_id, worker_id, session_name, action, status, attempts, "
    "created_at, claimed_at, lease_expires_at, completed_at, last_error"
)


def _row_to_assignment(row: Any) -> WahaSessionAssignment:
    return WahaSessionAssignment(
        session_name=row["session_name"],
        user_id=str(row["user_id"]),
        worker_id=row["worker_id"],
        status=row["status"],
        raw_status=row["raw_status"],
        last_status_at=row["last_status_at"],
        event_seq=row["event_seq"],
        pending_effect=row["pending_effect"],
        pending_effect_at=row["pending_effect_at"],
        last_qr_hash=row["last_qr_hash"],
        last_qr_image=row["last_qr_image"],
        qr_updated_at=row["qr_updated_at"],
        account_phone=row["account_phone"],
        account_identity_at=row["account_identity_at"],
    )


def _row_to_command(row: Any) -> WahaCommand:
    return WahaCommand(
        command_id=str(row["command_id"]),
        worker_id=row["worker_id"],
        session_name=row["session_name"],
        action=row["action"],
        status=row["status"],
        attempts=row["attempts"],
        created_at=row["created_at"],
        claimed_at=row["claimed_at"],
        lease_expires_at=row["lease_expires_at"],
        completed_at=row["completed_at"],
        last_error=row["last_error"],
    )


class PostgresWahaSessionRepository:
    """Owns ``waha_sessions`` and ``waha_commands`` (one WAHA aggregate)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # --- allocation ------------------------------------------------------------

    async def allocate_and_enqueue_create(
        self,
        user_id: str,
        *,
        heartbeat_timeout_seconds: float = 90.0,
    ) -> WahaSessionAssignment:
        """Atomically get-or-allocate the user's assignment and enqueue the
        ``create_and_start`` command.

        An existing assignment is returned as-is (stable session name +
        worker across attempts; an in-flight provisioning assignment is not
        a duplicate-user error), and a fresh ``create_and_start`` command is
        enqueued only when none is active for the session. Raises
        :class:`WahaAllocationError` when no eligible worker exists.
        """
        user_uuid = uuid.UUID(user_id)
        async with self._session_factory() as session:
            assignment = await self._get_or_allocate(
                session,
                user_uuid,
                heartbeat_timeout_seconds=heartbeat_timeout_seconds,
            )
            await self._enqueue(
                session,
                session_name=assignment.session_name,
                worker_id=assignment.worker_id,
                action="create_and_start",
            )
            await session.commit()
            return assignment

    async def _get_or_allocate(
        self,
        session: AsyncSession,
        user_uuid: uuid.UUID,
        *,
        heartbeat_timeout_seconds: float,
    ) -> WahaSessionAssignment:
        # Serialize allocation decisions: the eligibility read + insert must
        # be one atomic step so concurrent onboarding cannot overbook.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _ALLOCATION_LOCK_KEY},
        )

        existing = (
            (
                await session.execute(
                    text(f"{_SELECT_BY_USER} FOR UPDATE"),
                    {"user_id": user_uuid},
                )
            )
            .mappings()
            .first()
        )
        if existing is not None:
            return _row_to_assignment(existing)

        chosen = await self._select_worker(
            session,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        )
        session_name = f"echo-{uuid.uuid4()}"
        try:
            await session.execute(
                text(
                    "INSERT INTO waha_sessions"
                    " (session_name, user_id, worker_id, status, event_seq,"
                    "  created_at, updated_at)"
                    " VALUES (:session_name, :user_id, :worker_id,"
                    "  'provisioning', 0, :now, :now)"
                ),
                {
                    "session_name": session_name,
                    "user_id": user_uuid,
                    "worker_id": chosen,
                    "now": datetime.now(timezone.utc),
                },
            )
        except IntegrityError:
            # UNIQUE(user_id) race lost to a concurrent allocation (the
            # advisory lock should prevent this; the constraint is the
            # backstop). Re-read and reuse the winner's assignment.
            await session.rollback()
            raise
        return WahaSessionAssignment(
            session_name=session_name,
            user_id=str(user_uuid),
            worker_id=chosen,
            status="provisioning",
        )

    async def _select_worker(
        self,
        session: AsyncSession,
        *,
        heartbeat_timeout_seconds: float,
    ) -> str:
        """Pick the eligible worker with the lowest assigned-capacity ratio.

        Eligibility: fresh Echo-side heartbeat, ``health == 'ok'``, support
        for the agent management protocol (``management_protocol >= 1`` —
        batch-only workers never receive onboarding assignments), and
        Echo-assigned sessions below the reported ``max_sessions``. The
        tie-breaker is the worker id (deterministic). Raises
        :class:`WahaAllocationError` with diagnostic counts when nothing is
        eligible.
        """
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT w.worker_id, w.max_sessions, w.health,"
                        " w.last_seen_at, w.management_protocol,"
                        " COALESCE(a.assigned, 0) AS assigned"
                        " FROM waha_workers w"
                        " LEFT JOIN (SELECT worker_id, count(*) AS assigned"
                        "  FROM waha_sessions GROUP BY worker_id) a"
                        "  ON a.worker_id = w.worker_id"
                    )
                )
            )
            .mappings()
            .all()
        )
        now = datetime.now(timezone.utc)
        healthy: list[Any] = []
        manageable = 0
        eligible: list[tuple[float, str]] = []
        for row in rows:
            last_seen = row["last_seen_at"]
            fresh = (
                last_seen is not None
                and (now - last_seen).total_seconds() <= heartbeat_timeout_seconds
            )
            if not (fresh and row["health"] == "ok"):
                continue
            healthy.append(row)
            if row["management_protocol"] < 1:
                continue
            manageable += 1
            if row["assigned"] < row["max_sessions"] and row["max_sessions"] > 0:
                ratio = row["assigned"] / row["max_sessions"]
                eligible.append((ratio, row["worker_id"]))

        if not eligible:
            reason = (
                "no_registered_workers"
                if not rows
                else "no_healthy_workers"
                if not healthy
                else "no_management_agents"
                if manageable == 0
                else "no_capacity"
            )
            raise WahaAllocationError(
                reason,
                registered=len(rows),
                healthy=len(healthy),
                configured=manageable,
                with_capacity=len(eligible),
            )
        # min() over (ratio, worker_id) — lowest load wins, worker_id breaks
        # ties deterministically.
        return min(eligible)[1]

    # --- reads -------------------------------------------------------------------

    async def get(self, session_name: str) -> WahaSessionAssignment | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(_SELECT_BY_SESSION), {"session_name": session_name}
                    )
                )
                .mappings()
                .first()
            )
            return _row_to_assignment(row) if row else None

    async def get_by_user(self, user_id: str) -> WahaSessionAssignment | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(_SELECT_BY_USER), {"user_id": uuid.UUID(user_id)}
                    )
                )
                .mappings()
                .first()
            )
            return _row_to_assignment(row) if row else None

    # --- command queue ---------------------------------------------------------

    async def _enqueue(
        self,
        session: AsyncSession,
        *,
        session_name: str,
        worker_id: str,
        action: str,
    ) -> bool:
        """Insert a command unless an active one exists for (session,
        action). Runs inside the caller's transaction."""
        result = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    "INSERT INTO waha_commands"
                    " (worker_id, session_name, action, status, attempts)"
                    " SELECT :worker_id, :session_name, :action, 'pending', 0"
                    " WHERE NOT EXISTS ("
                    "  SELECT 1 FROM waha_commands"
                    "  WHERE session_name = :session_name AND action = :action"
                    "  AND status IN ('pending', 'claimed'))"
                    " RETURNING command_id"
                ),
                {
                    "worker_id": worker_id,
                    "session_name": session_name,
                    "action": action,
                },
            ),
        )
        return bool(result.rowcount)

    async def enqueue(
        self,
        session_name: str,
        *,
        worker_id: str,
        action: str,
    ) -> bool:
        """Enqueue a management command (no-op when one is already active).

        Returns ``True`` when a new command was created.
        """
        async with self._session_factory() as session:
            created = await self._enqueue(
                session, session_name=session_name, worker_id=worker_id, action=action
            )
            await session.commit()
            return created

    async def claim_for_worker(
        self,
        worker_id: str,
        *,
        limit: int = 10,
        lease_seconds: float = 120.0,
        max_attempts: int = 5,
    ) -> list[WahaCommand]:
        """Claim up to ``limit`` runnable commands for one worker.

        Claims pending rows and claimed rows whose lease expired, skipping
        rows whose attempts reached ``max_attempts`` (those are marked
        ``failed``). ``FOR UPDATE SKIP LOCKED`` guarantees concurrent polls
        never claim the same command independently. Claiming does not
        remove anything — completion is explicit and idempotent.
        """
        async with self._session_factory() as session:
            now = datetime.now(timezone.utc)
            await session.execute(
                text(
                    "UPDATE waha_commands SET status = 'failed',"
                    " completed_at = :now, last_error ="
                    " 'attempt limit reached'"
                    " WHERE status IN ('pending', 'claimed')"
                    " AND attempts >= :max_attempts"
                ),
                {"now": now, "max_attempts": max_attempts},
            )
            rows = (
                (
                    await session.execute(
                        text(
                            f"UPDATE waha_commands SET status = 'claimed',"
                            f" claimed_at = :now, attempts = attempts + 1,"
                            f" lease_expires_at = :lease"
                            f" WHERE command_id IN ("
                            f"  SELECT command_id FROM waha_commands"
                            f"  WHERE worker_id = :worker_id"
                            f"  AND (status = 'pending'"
                            f"       OR (status = 'claimed'"
                            f"           AND lease_expires_at < :now))"
                            f"  ORDER BY created_at"
                            f"  LIMIT :limit"
                            f"  FOR UPDATE SKIP LOCKED)"
                            f" RETURNING {_COMMAND_COLUMNS}"
                        ),
                        {
                            "worker_id": worker_id,
                            "now": now,
                            "lease": now + timedelta(seconds=lease_seconds),
                            "limit": limit,
                            "max_attempts": max_attempts,
                        },
                    )
                )
                .mappings()
                .all()
            )
            await session.commit()
            return [_row_to_command(row) for row in rows]

    async def complete_command(
        self,
        command_id: str,
        *,
        worker_id: str,
        success: bool,
        error: str | None = None,
    ) -> bool:
        """Mark a command completed/failed, guarded by its worker.

        Idempotent: an already-completed command is acknowledged (``True``)
        without changes. Returns ``False`` when the command does not exist
        or belongs to a different worker (never a caller-visible error —
        foreign results are rejected by the route layer first).
        """
        async with self._session_factory() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    text(
                        "UPDATE waha_commands SET status = :final,"
                        " completed_at = :now, last_error = :error"
                        " WHERE command_id = :command_id AND worker_id = :worker_id"
                        " AND status IN ('pending', 'claimed')"
                    ),
                    {
                        "final": "completed" if success else "failed",
                        "now": datetime.now(timezone.utc),
                        "error": error,
                        "command_id": uuid.UUID(command_id),
                        "worker_id": worker_id,
                    },
                ),
            )
            if bool(result.rowcount):
                await session.commit()
                return True
            existing = (
                await session.execute(
                    text(
                        "SELECT status FROM waha_commands"
                        " WHERE command_id = :command_id"
                        " AND worker_id = :worker_id"
                    ),
                    {
                        "command_id": uuid.UUID(command_id),
                        "worker_id": worker_id,
                    },
                )
            ).scalar_one_or_none()
            return existing in ("completed", "failed")

    async def active_commands(self, session_name: str) -> list[WahaCommand]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"SELECT {_COMMAND_COLUMNS} FROM waha_commands"
                            " WHERE session_name = :session_name"
                            " AND status IN ('pending', 'claimed')"
                            " ORDER BY created_at"
                        ),
                        {"session_name": session_name},
                    )
                )
                .mappings()
                .all()
            )
            return [_row_to_command(row) for row in rows]

    # --- lifecycle state machine --------------------------------------------------

    async def apply_lifecycle(
        self,
        session_name: str,
        *,
        raw_status: str,
        mapped_status: str,
        event_at: datetime,
        account_phone: str | None = None,
    ) -> WahaTransitionOutcome | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(f"{_SELECT_BY_SESSION} FOR UPDATE"),
                        {"session_name": session_name},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                await session.rollback()
                return None

            previous_status = row["status"]
            stored_at = row["last_status_at"]
            stale = stored_at is not None and event_at < stored_at
            identical = (
                not stale
                and row["status"] == mapped_status
                and row["raw_status"] == raw_status
                and stored_at == event_at
            )

            if stale or identical:
                # No state change: mirror the *stored* status onto the
                # connection row (idempotent; also self-heals rows created
                # after the transition landed) and surface any unfinished
                # pending effect so the caller can retry it. A replayed
                # (identical, never stale) WORKING event may still carry the
                # account identity — persist it idempotently so identity
                # reporting does not depend on a state change.
                if not stale and raw_status == "WORKING" and account_phone:
                    await self._set_account_identity(
                        session, session_name, account_phone, event_at
                    )
                connection_updated = await self._mirror_connection(
                    session, session_name, row["status"], row["raw_status"]
                )
                await session.commit()
                return WahaTransitionOutcome(
                    applied=False,
                    session_name=session_name,
                    status=row["status"],
                    raw_status=row["raw_status"],
                    previous_status=previous_status,
                    event_seq=row["event_seq"],
                    event_at=stored_at,
                    pending_effect=row["pending_effect"],
                    pending_effect_at=row["pending_effect_at"],
                    connection_updated=connection_updated,
                )

            # SCAN_QR_CODE queues a deliver_qr effect even though the QR
            # image itself arrives as its own agent event (qr_ready): the
            # queued effect is what makes a failed delivery retryable on
            # the next (even identical) event.
            effect = effect_for_waha_status(raw_status)
            # Account identity rides the same transaction as the lifecycle
            # transition: any applied transition into a non-WORKING state
            # (re-pairing, stop, failure) clears it so a stale identity can
            # never be reused after a new pairing; a WORKING transition
            # carrying the provider-reported phone sets it.
            if raw_status == "WORKING":
                identity_sql = (
                    ", account_phone = :account_phone,"
                    " account_identity_at = :identity_at"
                    if account_phone
                    else ""
                )
            else:
                identity_sql = ", account_phone = NULL, account_identity_at = NULL"
            await session.execute(
                text(
                    "UPDATE waha_sessions SET status = :status,"
                    " raw_status = :raw_status, last_status_at = :event_at,"
                    " event_seq = event_seq + 1, pending_effect = :effect,"
                    " pending_effect_at = :effect_at, updated_at = :now"
                    f"{identity_sql}"
                    " WHERE session_name = :session_name"
                ),
                {
                    "status": mapped_status,
                    "raw_status": raw_status,
                    "event_at": event_at,
                    "effect": effect,
                    "effect_at": event_at if effect else None,
                    "now": datetime.now(timezone.utc),
                    "session_name": session_name,
                    "account_phone": account_phone,
                    "identity_at": event_at if account_phone else None,
                },
            )
            connection_updated = await self._mirror_connection(
                session, session_name, mapped_status, raw_status
            )
            await session.commit()
            return WahaTransitionOutcome(
                applied=True,
                session_name=session_name,
                status=mapped_status,
                raw_status=raw_status,
                previous_status=previous_status,
                event_seq=row["event_seq"] + 1,
                event_at=event_at,
                pending_effect=effect,
                pending_effect_at=event_at if effect else None,
                connection_updated=connection_updated,
            )

    async def _set_account_identity(
        self,
        session: AsyncSession,
        session_name: str,
        account_phone: str,
        observed_at: datetime,
    ) -> None:
        await session.execute(
            text(
                "UPDATE waha_sessions SET account_phone = :account_phone,"
                " account_identity_at = :observed_at, updated_at = :now"
                " WHERE session_name = :session_name"
            ),
            {
                "account_phone": account_phone,
                "observed_at": observed_at,
                "now": datetime.now(timezone.utc),
                "session_name": session_name,
            },
        )

    async def _mirror_connection(
        self,
        session: AsyncSession,
        session_name: str,
        mapped_status: str,
        raw_status: str | None,
    ) -> bool:
        """Mirror a mapped status onto the ``whatsapp_connections`` row.

        Idempotent UPDATE; returns ``False`` when the connection row does
        not exist yet (pre-create callback window — the state stays in
        ``waha_sessions`` and the onboarding save path reconciles it).
        """
        result = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    "UPDATE whatsapp_connections SET connection_status = :status,"
                    " provider_raw_status = :raw_status, updated_at = :now"
                    " WHERE provider = 'waha'"
                    " AND provider_connection_id = :session_name"
                ),
                {
                    "status": mapped_status,
                    "raw_status": raw_status,
                    "now": datetime.now(timezone.utc),
                    "session_name": session_name,
                },
            ),
        )
        return bool(result.rowcount)

    async def clear_pending_effect(
        self,
        session_name: str,
        *,
        effect: str,
        effect_at: datetime,
    ) -> bool:
        async with self._session_factory() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    text(
                        "UPDATE waha_sessions SET pending_effect = NULL,"
                        " pending_effect_at = NULL, updated_at = :now"
                        " WHERE session_name = :session_name"
                        " AND pending_effect = :effect"
                        " AND pending_effect_at = :effect_at"
                    ),
                    {
                        "session_name": session_name,
                        "effect": effect,
                        "effect_at": effect_at,
                        "now": datetime.now(timezone.utc),
                    },
                ),
            )
            await session.commit()
            return bool(result.rowcount)

    async def record_qr(
        self,
        session_name: str,
        *,
        qr_hash: str,
        qr_image: str | None = None,
        effect_at: datetime,
    ) -> bool:
        """Record a delivered QR (hash + short-lived image) and clear the
        queued ``deliver_qr`` effect, fenced on ``effect_at``."""
        async with self._session_factory() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    text(
                        "UPDATE waha_sessions SET last_qr_hash = :qr_hash,"
                        " last_qr_image = :qr_image, qr_updated_at = :now,"
                        " pending_effect = NULL, pending_effect_at = NULL"
                        " WHERE session_name = :session_name"
                        " AND (pending_effect = 'deliver_qr'"
                        "      AND pending_effect_at = :effect_at)"
                    ),
                    {
                        "session_name": session_name,
                        "qr_hash": qr_hash,
                        "qr_image": qr_image,
                        "effect_at": effect_at,
                        "now": datetime.now(timezone.utc),
                    },
                ),
            )
            await session.commit()
            return bool(result.rowcount)

    async def allocated_counts(self) -> dict[str, int]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT worker_id, count(*) AS assigned"
                            " FROM waha_sessions GROUP BY worker_id"
                        )
                    )
                )
                .mappings()
                .all()
            )
            return {row["worker_id"]: row["assigned"] for row in rows}
