"""Persistence port for the WAHA worker registry.

``register`` is an idempotent upsert keyed by ``worker_id`` that preserves
the first-registration timestamp. Availability is derived at read time from
Echo's server-side ``last_seen_at`` (never from the worker's clock) combined
with reported ``health`` — no background task maintains it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from echo_v2.domain.waha import WahaWorkerRegistration, WahaWorkerStatus

__all__ = ["WahaWorkerRegistry"]


@runtime_checkable
class WahaWorkerRegistry(Protocol):
    async def register(self, registration: WahaWorkerRegistration) -> bool:
        """Upsert a worker. Returns ``True`` on first registration.

        Subsequent heartbeats update the reported state and Echo's
        ``last_seen_at`` while preserving ``first_registered_at``.
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
