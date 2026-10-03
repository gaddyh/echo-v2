"""Durable consumption of connector-owned Baileys state events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.integrations.baileys.events import BaileysConnectionStateEvent
from echo_v2.ports.whatsapp import ConnectionStatus


@dataclass(frozen=True)
class ClaimedBaileysEvent:
    event: BaileysConnectionStateEvent
    worker_id: str


class BaileysEventRepository:
    """Reads connector events and owns Echo's processing/side-effect ledger."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim_batch(
        self,
        worker_id: str,
        *,
        batch_size: int,
        lease_seconds: int,
        max_attempts: int = 10,
    ) -> list[ClaimedBaileysEvent]:
        now = datetime.now(timezone.utc)
        lease_cutoff = now - timedelta(seconds=lease_seconds)
        async with self._session_factory() as session, session.begin():
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT e.id, e.event_id, e.payload
                              FROM whatsapp_connector.event_inbox e
                             WHERE e.event_type = 'connection_state'
                               AND e.processed_at IS NULL
                               AND NOT EXISTS (
                                   SELECT 1
                                     FROM baileys_connector_event_processing p
                                    WHERE p.event_id = e.event_id
                                      AND p.status = 'processed'
                               )
                             ORDER BY e.received_at, e.id
                             LIMIT :batch_size
                             FOR UPDATE SKIP LOCKED
                            """
                        ),
                        {"batch_size": batch_size},
                    )
                ).mappings().all()
                claimed: list[ClaimedBaileysEvent] = []
                for row in rows:
                    event_id = str(row["event_id"])
                    claim = await session.execute(
                        text(
                            """
                            INSERT INTO baileys_connector_event_processing(
                                event_id, inbox_id, status, claimed_by, claimed_at,
                                attempt_count, updated_at
                            ) VALUES (:event_id, :inbox_id, 'claimed', :worker_id,
                                      :claimed_at, 1, :claimed_at)
                            ON CONFLICT (event_id) DO UPDATE
                              SET status = 'claimed', claimed_by = :worker_id,
                                  claimed_at = :claimed_at,
                                  attempt_count = baileys_connector_event_processing.attempt_count + 1,
                                  last_error = NULL, updated_at = :claimed_at
                            WHERE (baileys_connector_event_processing.status = 'failed'
                               OR (baileys_connector_event_processing.status = 'claimed'
                                   AND baileys_connector_event_processing.claimed_at < :lease_cutoff))
                              AND baileys_connector_event_processing.attempt_count < :max_attempts
                            RETURNING event_id
                            """
                        ),
                        {
                            "event_id": event_id,
                            "inbox_id": int(row["id"]),
                            "worker_id": worker_id,
                            "claimed_at": now,
                            "lease_cutoff": lease_cutoff,
                            "max_attempts": max_attempts,
                        },
                    )
                    if claim.scalar_one_or_none() is None:
                        continue
                    payload = row["payload"]
                    if isinstance(payload, str):  # pragma: no cover - JSONB decodes to dict
                        payload = json.loads(payload)
                    if not isinstance(payload, dict):  # pragma: no cover - JSONB object contract
                        raise TypeError(f"invalid connector payload for {event_id}")
                    claimed.append(
                        ClaimedBaileysEvent(
                            event=BaileysConnectionStateEvent.from_payload(
                                payload, int(row["id"])
                            ),
                            worker_id=worker_id,
                        )
                    )
                return claimed

    async def process_claimed_event(
        self,
        claimed: ClaimedBaileysEvent,
        *,
        notification_message: str,
    ) -> bool:
        """Apply a state event and enqueue an idempotent notification atomically."""
        event = claimed.event
        action_id = reauth_action_id(event.event_id)
        notification_key = reauth_notification_key(event.event_id)
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session, session.begin():
                ledger = (
                    await session.execute(
                        text(
                            """
                            SELECT status, claimed_by, claimed_at
                              FROM baileys_connector_event_processing
                             WHERE event_id = :event_id
                             FOR UPDATE
                            """
                        ),
                        {"event_id": event.event_id},
                    )
                ).mappings().one_or_none()
                if ledger is None or ledger["status"] == "processed":
                    return False
                if ledger["claimed_by"] != claimed.worker_id:
                    return False

                connection = (
                    await session.execute(
                        text(
                            """
                            SELECT c.id, c.user_id, c.connection_status,
                                   u.onboarding_status, u.phone_number
                              FROM whatsapp_connections c
                              JOIN users u ON u.id = c.user_id
                             WHERE c.provider = 'baileys'
                               AND c.provider_connection_id = :connection_id
                             FOR UPDATE
                            """
                        ),
                        {"connection_id": event.connection_id},
                    )
                ).mappings().one_or_none()
                if connection is None:
                    await self._fail(session, event.event_id, "unknown Echo Baileys connection")
                    return False

                previous = ConnectionStatus(str(connection["connection_status"]))
                should_notify = (
                    previous is ConnectionStatus.CONNECTED
                    and event.status is ConnectionStatus.PAIRING_REQUIRED
                    and connection["onboarding_status"] == "active"
                    and _is_real_auth_loss(event.provider_raw_status)
                )
                await session.execute(
                    text(
                        """
                        UPDATE whatsapp_connections
                           SET connection_status = :status,
                               provider_raw_status = :raw_status,
                               updated_at = :updated_at
                         WHERE id = :id
                        """
                    ),
                    {
                        "status": event.status.value,
                        "raw_status": event.provider_raw_status,
                        "updated_at": now,
                        "id": connection["id"],
                    },
                )
                if should_notify:
                    await session.execute(
                        text(
                            """
                            INSERT INTO scheduled_actions(
                                id, user_id, type, execute_at_utc, timezone, status,
                                payload, created_at, updated_at
                            ) VALUES (
                                :id, :user_id, 'send_bot_message', :execute_at,
                                'UTC', 'pending', CAST(:payload AS jsonb),
                                :created_at, :updated_at
                            ) ON CONFLICT (id) DO NOTHING
                            """
                        ),
                        {
                            "id": action_id,
                            "user_id": connection["user_id"],
                            "execute_at": now,
                            "payload": json.dumps(
                                {
                                    "chat_id": str(connection["phone_number"]),
                                    "message": notification_message,
                                    "idempotency_key": notification_key,
                                    "baileys_event_id": event.event_id,
                                }
                            ),
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                    notification_state = "pending"
                else:
                    notification_state = "none"
                await session.execute(
                    text(
                        """
                        UPDATE baileys_connector_event_processing
                           SET status = 'processed', processed_at = :processed_at,
                               notification_state = :notification_state,
                               updated_at = :updated_at
                         WHERE event_id = :event_id
                           AND claimed_by = :worker_id
                        """
                    ),
                    {
                        "processed_at": now,
                        "notification_state": notification_state,
                        "updated_at": now,
                        "event_id": event.event_id,
                        "worker_id": claimed.worker_id,
                    },
                )
                return should_notify

    async def mark_notification_sent(self, event_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                text(
                    """
                    UPDATE baileys_connector_event_processing
                       SET notification_state = 'sent', updated_at = now()
                     WHERE event_id = :event_id
                    """
                ),
                {"event_id": event_id},
            )
            await session.commit()

    async def mark_failed(self, event_id: str, worker_id: str, error: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                text(
                    """
                    UPDATE baileys_connector_event_processing
                       SET status = 'failed', last_error = :error,
                           updated_at = now()
                     WHERE event_id = :event_id AND claimed_by = :worker_id
                    """
                ),
                {"event_id": event_id, "worker_id": worker_id, "error": error[:2000]},
            )
            await session.commit()

    async def _fail(self, session: AsyncSession, event_id: str, error: str) -> None:
        await session.execute(
            text(
                """
                UPDATE baileys_connector_event_processing
                   SET status='failed', last_error=:error, updated_at=now()
                 WHERE event_id=:event_id
                """
            ),
            {"event_id": event_id, "error": error},
        )


def reauth_notification_key(event_id: str) -> str:
    return f"baileys-reauth:{event_id}"


def reauth_action_id(event_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, reauth_notification_key(event_id)))


def _is_real_auth_loss(raw_status: str | None) -> bool:
    return raw_status is not None and raw_status.startswith(("logged_out:", "device_removed:"))
