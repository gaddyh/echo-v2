"""Persistence port for WAHA session assignments + the agent command queue
(Phase 3, agent-based management).

Echo is the control plane: one row per user in ``waha_sessions`` — the
Echo-owned mapping of a user to a stable WAHA session name pinned to one
registered worker — plus a PostgreSQL-backed queue of management commands
(``waha_commands``) that the per-VM agent claims by polling and executes
against its local WAHA. Echo never connects to a worker, and no transaction
is held across agent execution.

Lifecycle state (``status`` / ``raw_status`` / ``last_status_at`` /
``event_seq``) is persisted **independently** of the ``whatsapp_connections``
row, so agent events arriving before the connection record is created are
kept. When the connection row exists, every applied transition is mirrored
onto it in the same transaction.

Side-effect reliability: a transition queues a ``pending_effect`` which stays
queued until explicitly cleared — a failed effect is re-returned by
``apply_lifecycle`` on the next (even duplicate) event, so state persistence
and effect execution are separately retryable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from echo_v2.domain.waha import (
    WahaCommand,
    WahaSessionAssignment,
    WahaTransitionOutcome,
)

__all__ = ["WahaSessionRepository"]


@runtime_checkable
class WahaSessionRepository(Protocol):
    async def allocate_and_enqueue_create(
        self,
        user_id: str,
        *,
        heartbeat_timeout_seconds: float = 90.0,
    ) -> WahaSessionAssignment:
        """Atomically get-or-allocate the user's stable assignment and
        enqueue the ``create_and_start`` command.

        An existing row is returned as-is (stable session name + worker,
        even while a previous create outcome is unresolved — that is not a
        duplicate-user error). A fresh row is allocated under an advisory
        lock among eligible workers (fresh heartbeat, ``health == "ok"``,
        ``management_protocol >= 1`` — batch-only agents are never assigned
        — and Echo-assigned count below ``max_sessions``), choosing the
        lowest assigned-capacity ratio with a deterministic ``worker_id``
        tie-breaker. Raises :class:`WahaAllocationError` when no worker is
        eligible. The whole operation (assignment + command) commits in one
        transaction — an assignment can never exist without an executable
        provisioning command.
        """
        ...

    async def get(self, session_name: str) -> WahaSessionAssignment | None: ...

    async def get_by_user(self, user_id: str) -> WahaSessionAssignment | None: ...

    # --- command queue -------------------------------------------------------

    async def enqueue(
        self,
        session_name: str,
        *,
        worker_id: str,
        action: str,
    ) -> bool:
        """Enqueue a management command for the assigned worker.

        No-op (returns ``False``) when an active command already exists for
        the same (session, action) — retries never duplicate work.
        """
        ...

    async def claim_for_worker(
        self,
        worker_id: str,
        *,
        limit: int = 10,
        lease_seconds: float = 120.0,
        max_attempts: int = 5,
    ) -> list[WahaCommand]:
        """Claim up to ``limit`` runnable commands for one worker.

        Claims pending rows and claimed rows whose lease expired
        (``FOR UPDATE SKIP LOCKED`` — concurrent polls never claim the same
        command independently), marking attempt-limit-exceeded rows
        ``failed``. Claiming does not remove commands; completion is
        explicit and idempotent.
        """
        ...

    async def complete_command(
        self,
        command_id: str,
        *,
        worker_id: str,
        success: bool,
        error: str | None = None,
    ) -> bool:
        """Mark a command completed/failed, guarded by its worker.

        Idempotent: duplicate results for an already-finished command
        return ``True`` without changes. Returns ``False`` when the command
        is unknown or belongs to a different worker.
        """
        ...

    async def active_commands(self, session_name: str) -> list[WahaCommand]:
        """Active (pending/claimed) commands for a session, oldest first."""
        ...

    # --- lifecycle state machine -------------------------------------------------

    async def apply_lifecycle(
        self,
        session_name: str,
        *,
        raw_status: str,
        mapped_status: str,
        event_at: datetime,
        account_phone: str | None = None,
    ) -> WahaTransitionOutcome | None:
        """Fence-apply one lifecycle event and queue its side effect.

        Row-locked. Events older than ``last_status_at`` are ignored
        (``applied=False``); an identical duplicate (same status +
        timestamp) does not re-apply but still returns any unfinished
        ``pending_effect`` so callers can retry it. A newer or changed event
        bumps ``event_seq``, updates the stored state, and queues the
        transition's effect (replacing any older unfinished one).

        The mapped status is mirrored onto ``whatsapp_connections`` when the
        row exists (idempotent). Returns ``None`` for unknown sessions.
        """
        ...

    async def clear_pending_effect(
        self,
        session_name: str,
        *,
        effect: str,
        effect_at: datetime,
    ) -> bool:
        """Clear a finished queued effect, fenced on its event timestamp.

        Returns ``False`` when the queued effect was replaced by a newer
        transition (nothing to clear) — the newer transition owns the state.
        """
        ...

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
        ...

    async def allocated_counts(self) -> dict[str, int]:
        """Echo-authoritative assigned-session counts per worker id.

        Worker-reported ``active_sessions`` is observational only; placement
        decisions use these counts.
        """
        ...
