"""Persistence port for the WAHA worker registry.

``register`` is an idempotent upsert keyed by ``worker_id`` that preserves
the first-registration timestamp. Availability is derived at read time from
Echo's server-side ``last_seen_at`` (never from the worker's clock) combined
with reported ``health`` — no background task maintains it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from echo_v2.domain.waha import (
    WahaRegistrationResult,
    WahaWorkerRegistration,
    WahaWorkerStatus,
)

__all__ = ["WahaWorkerRegistry"]


@runtime_checkable
class WahaWorkerRegistry(Protocol):
    async def register(
        self, registration: WahaWorkerRegistration
    ) -> WahaRegistrationResult:
        """Upsert a worker. ``first`` is ``True`` on first registration.

        Subsequent heartbeats update the reported state and Echo's
        ``last_seen_at`` while preserving ``first_registered_at``. When the
        registration declares management support and no token exists yet,
        a per-worker management token is issued (returned exactly once).
        """
        ...

    async def verify_worker_token(self, authorization: str | None) -> str | None:
        """Resolve a management bearer token to its worker id, or ``None``.

        Constant-time compare against the stored token hash; the worker id
        is taken from the token, never from a request body.
        """
        ...

    async def list_workers(
        self, *, heartbeat_timeout_seconds: float = 90.0
    ) -> list[WahaWorkerStatus]:
        """List workers with derived availability.

        A worker is ``available`` when its Echo-side heartbeat is fresher
        than ``heartbeat_timeout_seconds`` and it reports ``health ==
        "ok"``. Capacity (``has_capacity``) is reported separately.
        """
        ...
