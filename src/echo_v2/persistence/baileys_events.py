"""Durable consumption of connector-owned Baileys state events."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import CursorResult, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.integrations.baileys.events import BaileysConnectionStateEvent
from echo_v2.observability.privacy import correlation_id
from echo_v2.persistence.orm import PipelineEventRow
from echo_v2.ports.whatsapp import ConnectionStatus, ProviderMessageEvent

_logger = logging.getLogger("echo_v2.persistence.baileys_events")


@dataclass(frozen=True)
class BaileysMessageEnvelope:
    event_id: str
    inbox_id: int
    connection_id: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ClaimedBaileysEvent:
    event: Any
    worker_id: str


class BaileysEventRepository:
    """Reads connector events and owns Echo's processing/side-effect ledger."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    async def _record_reconnect_event(
        session: AsyncSession, name: str, *, event_id: str, child_id: str,
        status: str, request_id: str | None = None, state_version: int | None = None,
    ) -> None:
        try:
            child_hash = correlation_id(child_id)
        except RuntimeError:
            child_hash = None
        metadata: dict[str, str | int] = {"connection_status": status}
        if request_id is not None:
            metadata["request_id"] = request_id
        if state_version is not None:
            metadata["state_version"] = state_version
        await session.execute(pg_insert(PipelineEventRow).values(
            id=str(uuid4()), event_name=name, pipeline="guard_reconnect",
            status="recorded", event_id=event_id, user_id_hash=child_hash,
            event_metadata=metadata,
        ))
        _logger.info(
            "reconnect event=%s event_id=%s request_id=%s child_hash=%s connection_status=%s",
            name, event_id, request_id, child_hash, status,
        )

    async def claim_batch(
        self,
        worker_id: str,
        *,
        batch_size: int,
        lease_seconds: int,
        max_attempts: int = 10,
        event_type: str = "connection_state",
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
                             WHERE e.event_type = :event_type
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
                        {"batch_size": batch_size, "event_type": event_type},
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
                    if event_type == "message":
                        event: BaileysConnectionStateEvent | BaileysMessageEnvelope = (
                            BaileysMessageEnvelope(
                                event_id=event_id,
                                inbox_id=int(row["id"]),
                                connection_id=str(payload.get("connection_id", "")),
                                payload=payload,
                            )
                        )
                    else:
                        event = BaileysConnectionStateEvent.from_payload(
                            payload, int(row["id"])
                        )
                    claimed.append(ClaimedBaileysEvent(event=event, worker_id=worker_id))
                return claimed

    async def reconnect_child_for_event(
        self, connection_id: str, state_version: int | None
    ) -> str | None:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT c.user_id
                      FROM whatsapp_connections c
                     WHERE c.provider = 'baileys'
                       AND c.provider_connection_id = :connection_id
                       AND c.auth_loss_episode_key IS NOT NULL
                       AND (c.baileys_state_version IS NULL
                            OR (:state_version IS NOT NULL
                                AND c.baileys_state_version < :state_version))
                       AND EXISTS (
                           SELECT 1 FROM guard_reconnect_requests r
                            WHERE r.connection_id = c.id AND r.status = 'open'
                       )
                    """
                ),
                {"connection_id": connection_id, "state_version": state_version},
            )
            child_id = result.scalar_one_or_none()
            return str(child_id) if child_id is not None else None

    async def recent_connection_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            result = await session.execute(
                text("""
                    SELECT event_id, connection_id, payload, received_at, processed_at
                    FROM whatsapp_connector.event_inbox
                    WHERE event_type = 'connection_state'
                    ORDER BY received_at DESC
                    LIMIT :limit
                """),
                {"limit": max(1, min(limit, 500))},
            )
            return [dict(row) for row in result.mappings().all()]

    async def recent_processing_failures(self, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            result = await session.execute(
                text("""
                    SELECT event_id, status, attempt_count, last_error, updated_at
                    FROM baileys_connector_event_processing
                    WHERE status = 'failed' OR last_error IS NOT NULL
                    ORDER BY updated_at DESC
                    LIMIT :limit
                """),
                {"limit": max(1, min(limit, 200))},
            )
            return [dict(row) for row in result.mappings().all()]

    async def resolve_message_user(self, event: ProviderMessageEvent) -> tuple[str, str] | None:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT c.id, c.user_id
                      FROM whatsapp_connections c
                     WHERE c.provider = 'baileys'
                       AND c.provider_connection_id = :connection_id
                    """
                ),
                {"connection_id": event.connection.provider_connection_id},
            )
            row = result.mappings().one_or_none()
            if row is None:
                return None
            return str(row["user_id"]), str(row["id"])

    async def complete_claimed_event(
        self,
        claimed: ClaimedBaileysEvent,
    ) -> bool:
        """Mark a non-state event processed while retaining claim fencing."""
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session, session.begin():
            result = cast(CursorResult[Any], await session.execute(
                text(
                    """
                    UPDATE baileys_connector_event_processing
                       SET status = 'processed', processed_at = :processed_at,
                           notification_state = 'none', updated_at = :updated_at
                     WHERE event_id = :event_id
                       AND claimed_by = :worker_id
                       AND status = 'claimed'
                    """
                ),
                {
                    "processed_at": now,
                    "updated_at": now,
                    "event_id": claimed.event.event_id,
                    "worker_id": claimed.worker_id,
                },
            ))
            return result.rowcount == 1

    async def process_claimed_event(
        self,
        claimed: ClaimedBaileysEvent,
        *,
        notification_message: str,
    ) -> bool:
        """Apply a state event and enqueue guardian reconnect actions atomically."""
        del notification_message
        event = claimed.event
        if not isinstance(event, BaileysConnectionStateEvent):
            raise TypeError("connection-state processor received a message event")
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
                                   c.last_authenticated_at, c.auth_loss_episode_key,
                                   c.baileys_state_version,
                                   u.onboarding_status, u.phone_number, u.first_name
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

                last_version = connection["baileys_state_version"]
                if last_version is not None and (
                    event.state_version is None or event.state_version <= last_version
                ):
                    await self._record_reconnect_event(
                        session, "reconnect.state_ignored", event_id=event.event_id,
                        child_id=str(connection["user_id"]), status=event.status.value,
                        state_version=event.state_version,
                    )
                    await session.execute(
                        text(
                            """
                            UPDATE baileys_connector_event_processing
                               SET status = 'processed', processed_at = :processed_at,
                                   notification_state = 'none', updated_at = :processed_at
                             WHERE event_id = :event_id
                               AND claimed_by = :worker_id AND status = 'claimed'
                            """
                        ),
                        {"processed_at": now, "event_id": event.event_id,
                         "worker_id": claimed.worker_id},
                    )
                    return False

                auth_loss = (
                    event.status is ConnectionStatus.PAIRING_REQUIRED
                    and _is_real_auth_loss(event.provider_raw_status)
                )
                should_notify = (
                    auth_loss
                    and (
                        connection["last_authenticated_at"] is not None
                        or connection["connection_status"] == ConnectionStatus.CONNECTED.value
                    )
                    and connection["onboarding_status"] == "active"
                    and connection["auth_loss_episode_key"] is None
                )
                await session.execute(
                    text(
                        """
                        UPDATE whatsapp_connections
                           SET connection_status = :status,
                               provider_raw_status = :raw_status,
                               baileys_state_version = COALESCE(:state_version, baileys_state_version),
                               updated_at = :updated_at
                         WHERE id = :id
                        """
                    ),
                    {
                        "status": event.status.value,
                        "raw_status": event.provider_raw_status,
                        "state_version": event.state_version,
                        "updated_at": now,
                        "id": connection["id"],
                    },
                )
                created_actions = 0
                if auth_loss:
                    await self._record_reconnect_event(
                        session, "reconnect.auth_lost" if should_notify else "reconnect.auth_loss_duplicate",
                        event_id=event.event_id, child_id=str(connection["user_id"]),
                        status=event.status.value, state_version=event.state_version,
                    )
                elif connection["auth_loss_episode_key"] is not None:
                    await self._record_reconnect_event(
                        session, "reconnect.provider_state", event_id=event.event_id,
                        child_id=str(connection["user_id"]), status=event.status.value,
                        state_version=event.state_version,
                    )
                if should_notify:
                    episode = (
                        await session.execute(
                            text(
                                """
                                SELECT episode_key
                                  FROM guard_reconnect_requests
                                 WHERE child_user_id = :child_user_id
                                   AND status = 'open'
                                 ORDER BY created_at DESC
                                 LIMIT 1
                                 FOR UPDATE
                                """
                            ),
                            {"child_user_id": connection["user_id"]},
                        )
                    ).scalar_one_or_none()
                    episode_key = str(episode or f"baileys-auth-loss:{event.connection_id}:{event.event_id}")
                    await session.execute(
                        text("UPDATE whatsapp_connections SET auth_loss_episode_key = :episode_key WHERE id = :id"),
                        {"episode_key": episode_key, "id": connection["id"]},
                    )
                    guardians = (
                        await session.execute(
                            text(
                                """
                                SELECT l.guardian_user_id, u.phone_number
                                  FROM guardian_child_links l
                                  JOIN users u ON u.id = l.guardian_user_id
                                 WHERE l.child_user_id = :child_user_id
                                   AND l.status = 'active'
                                   AND l.guardian_authorized_at IS NOT NULL
                                   AND l.safety_enabled_at IS NOT NULL
                                   AND u.onboarding_status = 'active'
                                """
                            ),
                            {"child_user_id": connection["user_id"]},
                        )
                    ).mappings().all()
                    for guardian in guardians:
                        request_id = uuid4()
                        request = (
                            await session.execute(
                                text(
                                    """
                                    INSERT INTO guard_reconnect_requests(
                                        id, child_user_id, guardian_user_id, episode_key,
                                        connection_id, status, created_at, updated_at
                                    ) VALUES (
                                        :id, :child_user_id, :guardian_user_id, :episode_key,
                                        :connection_id, 'open', :now, :now
                                    )
                                    ON CONFLICT (guardian_user_id, child_user_id, episode_key)
                                    DO UPDATE SET updated_at = EXCLUDED.updated_at
                                    RETURNING id
                                    """
                                ),
                                {
                                    "id": request_id,
                                    "child_user_id": connection["user_id"],
                                    "guardian_user_id": guardian["guardian_user_id"],
                                    "episode_key": episode_key,
                                    "connection_id": connection["id"],
                                    "now": now,
                                },
                            )
                        ).scalar_one()
                        action_id = reauth_action_id(str(request))
                        notification_key = reauth_notification_key(str(request))
                        action = cast(CursorResult[Any], await session.execute(
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
                                "user_id": guardian["guardian_user_id"],
                                "execute_at": now,
                                "payload": json.dumps(
                                    {
                                        "chat_id": str(guardian["phone_number"]),
                                        "message": "",
                                        "template": {
                                            "name": "guard_reconnect_v1",
                                            "language": "he",
                                            "body_params": [str(connection["first_name"] or "הילד/ה")],
                                            "quick_reply_payload": f"guard:reconnect:{request}",
                                        },
                                        "idempotency_key": notification_key,
                                        "baileys_event_id": event.event_id,
                                        "reconnect_request_id": str(request),
                                        "reconnect_child_id": str(connection["user_id"]),
                                    }
                                ),
                                "created_at": now,
                                "updated_at": now,
                            },
                        ))
                        created_actions += int(action.rowcount or 0)
                        if action.rowcount:
                            await self._record_reconnect_event(
                                session, "reconnect.notification_queued", event_id=event.event_id,
                                child_id=str(connection["user_id"]), status=event.status.value,
                                request_id=str(request), state_version=event.state_version,
                            )
                notification_state = "pending" if created_actions else "none"
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

    async def list_open_reconnect_requests(self, guardian_user_id: str) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT r.id, r.child_user_id, r.episode_key, r.connection_id,
                           c.connection_status, child.first_name, r.status, r.guardian_user_id
                      FROM guard_reconnect_requests r
                      JOIN whatsapp_connections c ON c.id = r.connection_id
                      JOIN users child ON child.id = r.child_user_id
                     WHERE r.guardian_user_id = :guardian_user_id
                       AND r.status = 'open'
                     ORDER BY r.created_at DESC
                    """
                ),
                {"guardian_user_id": guardian_user_id},
            )
            return [dict(row) for row in result.mappings().all()]

    async def list_open_reconnect_requests_for_child(self, child_user_id: str) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT r.id, r.child_user_id, r.guardian_user_id, r.episode_key,
                           r.connection_id, r.status, c.connection_status
                      FROM guard_reconnect_requests r
                      JOIN whatsapp_connections c ON c.id = r.connection_id
                     WHERE r.child_user_id = :child_user_id AND r.status = 'open'
                    """
                ),
                {"child_user_id": child_user_id},
            )
            return [dict(row) for row in result.mappings().all()]

    async def get_reconnect_request(self, request_id: str) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT r.id, r.child_user_id, r.guardian_user_id, r.episode_key,
                           r.connection_id, r.status, c.connection_status, child.first_name
                      FROM guard_reconnect_requests r
                      JOIN whatsapp_connections c ON c.id = r.connection_id
                      JOIN users child ON child.id = r.child_user_id
                     WHERE r.id = :request_id
                    """
                ),
                {"request_id": request_id},
            )
            row = result.mappings().one_or_none()
            return dict(row) if row else None

    async def close_reconnect_episode(self, child_user_id: str, episode_key: str) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    UPDATE guard_reconnect_requests
                       SET status = 'closed', closed_at = now(), updated_at = now()
                     WHERE child_user_id = :child_user_id
                       AND episode_key = :episode_key
                       AND status = 'open'
                    """
                ),
                {"child_user_id": child_user_id, "episode_key": episode_key},
            )

    async def close_open_reconnect_requests(self, child_user_id: str) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    UPDATE guard_reconnect_requests
                       SET status = 'closed', closed_at = now(), updated_at = now()
                     WHERE child_user_id = :child_user_id AND status = 'open'
                    """
                ),
                {"child_user_id": child_user_id},
            )

    async def record_reconnect_event(
        self, name: str, *, child_id: str, request_id: str | None = None,
        status: str = "recorded",
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await self._record_reconnect_event(
                session, name, event_id=request_id or "reconnect", child_id=child_id,
                status=status, request_id=request_id,
            )

    async def mark_verified_child_connection(self, child_user_id: str) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    UPDATE whatsapp_connections
                       SET last_authenticated_at = now(), updated_at = now()
                     WHERE user_id = :child_user_id AND provider = 'baileys'
                    """
                ),
                {"child_user_id": child_user_id},
            )

    async def complete_verified_reconnect(self, child_user_id: str) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    UPDATE whatsapp_connections
                       SET auth_loss_episode_key = NULL, last_authenticated_at = now(),
                           updated_at = now()
                     WHERE user_id = :child_user_id AND provider = 'baileys'
                       AND auth_loss_episode_key IS NOT NULL
                    """
                ),
                {"child_user_id": child_user_id},
            )
            await session.execute(
                text(
                    """
                    UPDATE guard_reconnect_requests
                       SET status = 'closed', closed_at = now(), updated_at = now()
                     WHERE child_user_id = :child_user_id AND status = 'open'
                    """
                ),
                {"child_user_id": child_user_id},
            )

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
