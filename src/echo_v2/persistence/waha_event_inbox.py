"""Persistence port for the WAHA event inbox.

``ingest_batch`` persists an entire batch in a single PostgreSQL
transaction (all-or-nothing) and classifies each message:

* new ``(session, message_id)`` → insert, status ``pending``;
* same key + same ``sha256`` → duplicate, no state reset, no new row;
* same key + different ``sha256`` → update payload, ``revision + 1``,
  status reset to ``pending``.

The ACK response is only returned after the transaction commits — never
before. ``mark_processed`` is revision-guarded so a processor that handled
an older revision can never mark a newer one processed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from echo_v2.domain.waha import (
    ClaimedWahaMessage,
    WahaBatchIngestResult,
    WahaInboxMessage,
)

__all__ = ["WahaEventInboxRepository"]


@runtime_checkable
class WahaEventInboxRepository(Protocol):
    async def ingest_batch(
        self, batch_id: str, messages: Sequence[WahaInboxMessage]
    ) -> WahaBatchIngestResult:
        """Atomically persist a batch. Must commit before returning."""
        ...

    async def get(self, session: str, message_id: str) -> WahaInboxMessage | None:
        """Return the stored message for ``(session, message_id)``, if any."""
        ...

    async def claim_pending(
        self,
        worker_id: str,
        *,
        batch_size: int,
        lease_seconds: int,
        max_attempts: int = 10,
    ) -> list[ClaimedWahaMessage]:
        """Claim up to ``batch_size`` retryable rows for processing.

        Claims pending/failed rows and expired ``processing`` leases,
        skipping rows whose ``attempt_count`` reached ``max_attempts``.
        Each claim bumps ``attempt_count`` and holds a lease.
        """
        ...

    async def complete_claimed(self, claimed: ClaimedWahaMessage) -> bool:
        """Mark a claimed row processed, fenced by revision and lease owner.

        Returns ``False`` (and leaves the row retryable) when the payload was
        updated mid-processing or the lease was lost.
        """
        ...

    async def complete_claimed_many(
        self, claimed: Sequence[ClaimedWahaMessage]
    ) -> int:
        """Mark several claimed rows processed in a single statement.

        Per-row fencing is identical to ``complete_claimed`` (revision +
        processing status + lease owner must match); rows that no longer
        qualify stay retryable. Returns the number completed. Used to drain
        suppressed history-sync rows without a round-trip per row.
        """
        ...

    async def mark_failed(self, claimed: ClaimedWahaMessage, error: str) -> None:
        """Release a claimed row as ``failed`` so a later poll can retry it."""
        ...

    async def backlog_stats(
        self,
    ) -> tuple[int, int, float | None]:
        """Return ``(pending, failed, oldest_age_seconds)`` for unprocessed rows.

        ``oldest_age_seconds`` is ``None`` when the inbox has no unprocessed
        rows. Media CDN URLs expire (~hours), so a growing age is an
        operational warning, not just lag.
        """
        ...

    async def mark_processed(
        self, session: str, message_id: str, *, revision: int
    ) -> bool:
        """Mark a message processed for exactly ``revision``.

        Returns ``True`` if the row was still at that revision and
        pending/processing; ``False`` if a newer revision arrived (the
        processor's work is stale and must be repeated for the new revision).
        """
        ...
