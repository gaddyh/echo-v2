"""Database-backed Baileys connector event consumption tests."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from echo_v2.integrations.baileys.events import BaileysMessageEvent
from echo_v2.persistence.baileys_events import BaileysEventRepository
from echo_v2.ports.whatsapp import ConnectionStatus

pytestmark = pytest.mark.asyncio


async def _create_connector_tables(session_factory) -> None:
    async with session_factory() as session:
        await session.execute(text("CREATE SCHEMA IF NOT EXISTS whatsapp_connector"))
        await session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS whatsapp_connector.connections (
                    id uuid PRIMARY KEY,
                    status text NOT NULL DEFAULT 'provisioning',
                    raw_status text,
                    state_version bigint NOT NULL DEFAULT 0
                )
                """
            )
        )
        await session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS whatsapp_connector.event_inbox (
                    id bigserial PRIMARY KEY,
                    event_id text NOT NULL,
                    connection_id uuid NOT NULL
                        REFERENCES whatsapp_connector.connections(id) ON DELETE CASCADE,
                    event_type text NOT NULL,
                    payload jsonb NOT NULL,
                    received_at timestamptz NOT NULL DEFAULT now(),
                    processed_at timestamptz,
                    UNIQUE(connection_id, event_id)
                )
                """
            )
        )
        await session.commit()


async def test_consumer_claims_updates_and_enqueues_once(
    session_factory,
    clean_db,
):
    await _create_connector_tables(session_factory)
    user_id = str(uuid.uuid4())
    connection_id = str(uuid.uuid4())
    connector_id = str(uuid.uuid4())
    event_id = f"state:{connector_id}:42"
    payload = {
        "schema_version": 1,
        "event_type": "connection_state",
        "event_id": event_id,
        "provider": "baileys",
        "connection_id": connector_id,
        "status": "pairing_required",
        "provider_raw_status": "logged_out:401",
        "state_version": 42,
        "timestamp": "2026-10-03T00:00:00Z",
    }

    async with session_factory() as session:
        await session.execute(
            text("INSERT INTO users(id, phone_number, onboarding_status) VALUES (:id, :phone, 'active')"),
            {"id": user_id, "phone": "972500000001"},
        )
        await session.execute(
            text(
                """
                INSERT INTO whatsapp_connections(
                    id, user_id, provider, provider_connection_id, credentials,
                    webhook_token_hash, connection_status, provider_raw_status
                ) VALUES(
                    :id, :user_id, 'baileys', :provider_id, :credentials,
                    :webhook_hash, 'connected', 'open'
                )
                """
            ),
            {
                "id": connection_id,
                "user_id": user_id,
                "provider_id": connector_id,
                "credentials": b"",
                "webhook_hash": b"",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO whatsapp_connector.connections(id, status, raw_status)
                VALUES (:id, 'pairing_required', 'logged_out:401')
                """
            ),
            {"id": connector_id},
        )
        await session.execute(
            text(
                """
                INSERT INTO whatsapp_connector.event_inbox(
                    event_id, connection_id, event_type, payload
                ) VALUES (:event_id, :connection_id, 'connection_state', CAST(:payload AS jsonb))
                """
            ),
            {
                "event_id": event_id,
                "connection_id": connector_id,
                "payload": json.dumps(payload),
            },
        )
        await session.commit()

    repository = BaileysEventRepository(session_factory)
    claimed = await repository.claim_batch(
        "test-worker", batch_size=50, lease_seconds=60
    )
    assert len(claimed) == 1
    assert claimed[0].event.status is ConnectionStatus.PAIRING_REQUIRED
    assert await repository.claim_batch(
        "second-worker", batch_size=50, lease_seconds=60
    ) == []

    assert await repository.process_claimed_event(
        claimed[0], notification_message="reauth"
    ) is True
    assert await repository.claim_batch(
        "test-worker-2", batch_size=50, lease_seconds=60
    ) == []

    async with session_factory() as session:
        status = (
            await session.execute(
                text(
                    "SELECT connection_status FROM whatsapp_connections WHERE id=:id"
                ),
                {"id": connection_id},
            )
        ).scalar_one()
        action = (
            await session.execute(
                text(
                    "SELECT payload FROM scheduled_actions WHERE user_id=:user_id"
                ),
                {"user_id": user_id},
            )
        ).scalar_one()
        ledger = (
            await session.execute(
                text(
                    "SELECT status, notification_state FROM baileys_connector_event_processing WHERE event_id=:event_id"
                ),
                {"event_id": event_id},
            )
        ).one()

    assert status == "pairing_required"
    assert action["idempotency_key"] == f"baileys-reauth:{event_id}"
    assert ledger == ("processed", "pending")
    await repository.mark_notification_sent(event_id)
    async with session_factory() as session:
        assert (
            await session.execute(
                text(
                    "SELECT notification_state FROM baileys_connector_event_processing WHERE event_id=:event_id"
                ),
                {"event_id": event_id},
            )
        ).scalar_one() == "sent"
        connected_event_id = f"state:{connector_id}:43"
        connected_payload = {**payload, "event_id": connected_event_id, "status": "connected", "provider_raw_status": "open", "state_version": 43}
        await session.execute(
            text(
                "INSERT INTO whatsapp_connector.event_inbox(event_id,connection_id,event_type,payload) VALUES (:event_id,:connection_id,'connection_state',CAST(:payload AS jsonb))"
            ),
            {"event_id": connected_event_id, "connection_id": connector_id, "payload": json.dumps(connected_payload)},
        )
        await session.commit()
    connected_claim = (await repository.claim_batch("worker-3", batch_size=50, lease_seconds=60))[0]
    assert await repository.process_claimed_event(connected_claim, notification_message="reauth") is False


async def test_message_event_claim_and_connection_resolution(session_factory, clean_db):
    await _create_connector_tables(session_factory)
    user_id = str(uuid.uuid4())
    connection_id = str(uuid.uuid4())
    connector_id = str(uuid.uuid4())
    event_id = f"message:{connector_id}:message:in"
    payload = {
        "schema_version": 1,
        "event_type": "message",
        "event_id": event_id,
        "provider": "baileys",
        "connection_id": connector_id,
        "chat_id": "15551234567@s.whatsapp.net",
        "is_group": False,
        "provider_message_id": "message",
        "direction": "inbound",
        "source": None,
        "timestamp": "2026-10-03T00:00:00Z",
        "kind": "text",
        "text": "hello",
    }
    async with session_factory() as session:
        await session.execute(
            text("INSERT INTO users(id, phone_number, onboarding_status) VALUES (:id, :phone, 'active')"),
            {"id": user_id, "phone": "972500000002"},
        )
        await session.execute(
            text(
                """
                INSERT INTO whatsapp_connections(
                    id, user_id, provider, provider_connection_id, credentials,
                    webhook_token_hash, connection_status, provider_raw_status
                ) VALUES(:id, :user_id, 'baileys', :provider_id, :credentials, :hash, 'connected', 'open')
                """
            ),
            {"id": connection_id, "user_id": user_id, "provider_id": connector_id, "credentials": b"", "hash": b""},
        )
        await session.execute(
            text("INSERT INTO whatsapp_connector.connections(id, status) VALUES (:id, 'connected')"),
            {"id": connector_id},
        )
        await session.execute(
            text(
                """
                INSERT INTO whatsapp_connector.event_inbox(event_id, connection_id, event_type, payload)
                VALUES (:event_id, :connection_id, 'message', CAST(:payload AS jsonb))
                """
            ),
            {"event_id": event_id, "connection_id": connector_id, "payload": json.dumps(payload)},
        )
        await session.commit()

    repository = BaileysEventRepository(session_factory)
    claimed = await repository.claim_batch(
        "message-worker", batch_size=10, lease_seconds=60, event_type="message"
    )
    assert len(claimed) == 1
    envelope = claimed[0].event
    event = BaileysMessageEvent.from_payload(envelope.payload)
    assert await repository.resolve_message_user(event) == (user_id, connection_id)
    assert await repository.complete_claimed_event(claimed[0]) is True
