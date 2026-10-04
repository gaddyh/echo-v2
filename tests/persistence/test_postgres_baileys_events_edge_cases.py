from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from echo_v2.persistence.baileys_events import (
    BaileysEventRepository,
    BaileysMessageEnvelope,
    ClaimedBaileysEvent,
)
from echo_v2.ports.whatsapp import ConnectionRef, MessageDirection, ProviderMessageEvent
from tests.persistence.test_postgres_baileys_events import _create_connector_tables


async def test_message_resolution_and_state_processor_reject_message(session_factory, clean_db):
    await _create_connector_tables(session_factory)
    repository = BaileysEventRepository(session_factory)
    event = ProviderMessageEvent(
        event_id="message:unknown",
        connection=ConnectionRef("baileys", str(uuid.uuid4())),
        chat_id="chat@s.whatsapp.net",
        provider_message_id="message",
        direction=MessageDirection.INBOUND,
        source=None,
        timestamp=datetime.now(timezone.utc),
    )
    assert await repository.resolve_message_user(event) is None
    claimed = ClaimedBaileysEvent(
        event=BaileysMessageEnvelope("message:unknown", 1, "unknown", {}),
        worker_id="worker",
    )
    with pytest.raises(TypeError, match="message event"):
        await repository.process_claimed_event(claimed, notification_message="ignored")


async def test_unknown_connection_and_lease_owner_are_safe(session_factory, clean_db):
    await _create_connector_tables(session_factory)
    connector_id = str(uuid.uuid4())
    event_id = f"state:{connector_id}:1"
    payload = {
        "schema_version": 1,
        "event_type": "connection_state",
        "event_id": event_id,
        "provider": "baileys",
        "connection_id": connector_id,
        "status": "pairing_required",
        "provider_raw_status": "logged_out:401",
        "timestamp": "2026-10-03T00:00:00Z",
    }
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO whatsapp_connector.connections(id, status) VALUES (:id, 'pairing_required')"
            ),
            {"id": connector_id},
        )
        await session.execute(
            text(
                """
                INSERT INTO whatsapp_connector.event_inbox(event_id, connection_id, event_type, payload)
                VALUES (:event_id, :connection_id, 'connection_state', CAST(:payload AS jsonb))
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
    claimed = (await repository.claim_batch("owner", batch_size=10, lease_seconds=60))[0]
    assert await repository.process_claimed_event(
        replace(claimed, worker_id="other"), notification_message="reauth"
    ) is False
    assert await repository.process_claimed_event(
        claimed, notification_message="reauth"
    ) is False
    missing_ledger = replace(
        claimed,
        event=replace(claimed.event, event_id=f"{event_id}:missing"),
    )
    assert await repository.process_claimed_event(
        missing_ledger, notification_message="reauth"
    ) is False
    await repository.mark_failed(event_id, "owner", "unknown connection")

    async with session_factory() as session:
        assert (
            await session.execute(
                text(
                    "SELECT status, last_error FROM baileys_connector_event_processing WHERE event_id=:event_id"
                ),
                {"event_id": event_id},
            )
        ).one() == ("failed", "unknown connection")
