"""PostgreSQL implementation of :class:`StateWebhookRepository`.

Combines dedup claim + connection status update in a single transaction,
eliminating the crash boundary that existed when they were called as
separate standalone transactions.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.persistence.orm import (
    ProviderWebhookEventRow,
    WhatsAppConnectionRow,
)
from echo_v2.persistence.state_webhook import (
    StateWebhookRepository,
    StateWebhookResult,
)
from echo_v2.ports.whatsapp import ConnectionRef, ConnectionStatus

__all__ = ["PostgresStateWebhookRepository"]


class PostgresStateWebhookRepository:
    """PostgreSQL implementation of :class:`StateWebhookRepository`.

    One transaction:
    1. Insert dedup row (ON CONFLICT DO NOTHING).
    2. If duplicate: rollback, return claimed=False.
    3. If new: fetch prior connection, update status, commit.
    4. Return claimed=True and was_connected (prior state).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

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
        async with self._session_factory() as session:
            try:
                # 1. Claim dedup row (ON CONFLICT DO NOTHING).
                claim_stmt = (
                    pg_insert(ProviderWebhookEventRow)
                    .values(
                        event_id=event_id,
                        provider=provider,
                        connection_id=connection_id,
                        event_type=event_type,
                    )
                    .on_conflict_do_nothing(index_elements=["event_id"])
                    .returning(ProviderWebhookEventRow.event_id)
                )
                result = await session.execute(claim_stmt)
                if result.scalar_one_or_none() is None:
                    # Duplicate — no status update.
                    await session.rollback()
                    return StateWebhookResult(claimed=False)

                # 2. Fetch prior connection status.
                sel_stmt = select(
                    WhatsAppConnectionRow.connection_status
                ).where(
                    WhatsAppConnectionRow.provider == connection_ref.provider,
                    WhatsAppConnectionRow.provider_connection_id
                    == connection_ref.provider_connection_id,
                )
                prior = (await session.execute(sel_stmt)).scalar_one_or_none()
                was_connected = (
                    prior is not None
                    and prior == ConnectionStatus.CONNECTED.value
                )

                # 3. Update connection status.
                now = datetime.now(timezone.utc)
                upd_stmt = (
                    update(WhatsAppConnectionRow)
                    .where(
                        WhatsAppConnectionRow.provider
                        == connection_ref.provider,
                        WhatsAppConnectionRow.provider_connection_id
                        == connection_ref.provider_connection_id,
                    )
                    .values(
                        connection_status=status.value,
                        provider_raw_status=raw,
                        updated_at=now,
                    )
                )
                await session.execute(upd_stmt)

                await session.commit()
                return StateWebhookResult(
                    claimed=True, was_connected=was_connected
                )
            except Exception:
                await session.rollback()
                raise


# Structural check: PostgresStateWebhookRepository satisfies the protocol.
_StateWebhookRepository_check: StateWebhookRepository = (
    PostgresStateWebhookRepository  # type: ignore[assignment]
)
