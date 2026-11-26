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

from echo_v2.domain.waha import WahaBatchIngestResult, WahaInboxMessage

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

    async def mark_processed(
        self, session: str, message_id: str, *, revision: int
    ) -> bool:
        """Mark a message processed for exactly ``revision``.

        Returns ``True`` if the row was still at that revision and
        pending/processing; ``False`` if a newer revision arrived (the
        processor's work is stale and must be repeated for the new revision).
        """
        ...
