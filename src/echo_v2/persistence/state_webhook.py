"""Atomic state-webhook dedup + connection status update.

Combines :class:`WebhookDedupStore.claim` and
:class:`WhatsAppConnectionRepository.update_status` into a single
transaction so a crash between them can never permanently lose a state
transition (the dedup row commits but the status update is lost, causing
retries to be rejected as duplicates forever).

The :class:`PostgresUnitOfWork` docstring describes this exact hazard.
This module provides the explicit atomic operation the user prefers
over the generic UoW — one method, one transaction, all-or-nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from echo_v2.ports.whatsapp import ConnectionRef, ConnectionStatus

if TYPE_CHECKING:
    from echo_v2.app.webhooks.dedup import WebhookDedupStore
    from echo_v2.persistence.whatsapp_connections import (
        WhatsAppConnectionRepository,
    )

__all__ = [
    "InMemoryStateWebhookRepository",
    "StateWebhookRepository",
    "StateWebhookResult",
]


@dataclass(frozen=True)
class StateWebhookResult:
    """Result of an atomic claim + status update.

    Attributes:
        claimed: ``True`` if this was a new claim (not a duplicate).
            When ``False``, the connection status was NOT updated.
        was_connected: ``True`` if the connection was ``CONNECTED``
            before the update. Used by the dispatcher to decide
            whether to send a disconnect notification.
    """

    claimed: bool
    was_connected: bool = False


@runtime_checkable
class StateWebhookRepository(Protocol):
    """Atomic dedup claim + connection status update.

    A single transaction:
    1. Insert dedup row (``ON CONFLICT DO NOTHING``).
    2. If duplicate: rollback, return ``claimed=False``.
    3. If new: fetch prior connection, update status, commit.
    4. Return ``claimed=True`` and ``was_connected`` (prior state).
    """

    async def claim_and_update_status(
        self,
        *,
        event_id: str,
        provider: str,
        connection_id: str,
        event_type: str,
        connection_ref: ConnectionRef,
        status: ConnectionStatus,
        raw: str | None,
    ) -> StateWebhookResult:
        """Claim the dedup row + update connection status, atomically.

        Returns ``StateWebhookResult(claimed=False)`` if the event was
        already processed (duplicate). Returns
        ``StateWebhookResult(claimed=True, was_connected=...)`` if the
        event was newly claimed and the status was updated.
        """
        ...


class InMemoryStateWebhookRepository:
    """In-memory :class:`StateWebhookRepository`.

    Delegates to :class:`InMemoryWebhookDedupStore` and
    :class:`InMemoryWhatsAppConnectionRepository`. Since in-memory
    operations are process-local, the "atomicity" is trivial.
    """

    def __init__(
        self,
        dedup_store: WebhookDedupStore,
        connection_repo: WhatsAppConnectionRepository,
    ) -> None:
        self._dedup = dedup_store
        self._connection_repo = connection_repo

    async def claim_and_update_status(
        self,
        *,
        event_id: str,
        provider: str,
        connection_id: str,
        event_type: str,
        connection_ref: ConnectionRef,
        status: ConnectionStatus,
        raw: str | None,
    ) -> StateWebhookResult:
        claimed = await self._dedup.claim(
            event_id,
            provider=provider,
            connection_id=connection_id,
            event_type=event_type,
        )
        if not claimed:
            return StateWebhookResult(claimed=False)

        conn_before = await self._connection_repo.get(connection_ref)
        was_connected = (
            conn_before is not None
            and conn_before.status == ConnectionStatus.CONNECTED
        )
        await self._connection_repo.update_status(connection_ref, status, raw)
        return StateWebhookResult(claimed=True, was_connected=was_connected)
