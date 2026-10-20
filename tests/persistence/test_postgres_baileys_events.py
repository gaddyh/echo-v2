"""Database-backed Baileys connector event consumption tests."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from echo_v2.integrations.baileys.events import BaileysMessageEvent
from echo_v2.persistence.baileys_events import BaileysEventRepository
from echo_v2.persistence.postgres_guard import PostgresGuardianChildLinkRepository
from echo_v2.persistence.postgres_whatsapp_connections import (
    PostgresWhatsAppConnectionRepository,
)
from echo_v2.persistence.user_repository import PostgresUserRepository
from echo_v2.ports.whatsapp import ConnectionStatus, ConnectionStatusSnapshot
from echo_v2.services.onboarding import OnboardingService

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
    guardian_id = str(uuid.uuid4())
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
            text("INSERT INTO users(id, phone_number, onboarding_status) VALUES (:id, :phone, 'active')"),
            {"id": guardian_id, "phone": "972500000009"},
        )
        await session.execute(
            text("INSERT INTO guardian_child_links(guardian_user_id, child_user_id, status, guardian_authorized_at, safety_enabled_at) VALUES (:guardian, :child, 'active', now(), now())"),
            {"guardian": guardian_id, "child": user_id},
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
                {"user_id": guardian_id},
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
    assert action["template"]["name"] == "guard_reconnect_v1"
    assert action["reconnect_request_id"]
    assert action["idempotency_key"].startswith("baileys-reauth:")
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


async def test_recent_event_queries(session_factory, clean_db):
    repository = BaileysEventRepository(session_factory)
    assert isinstance(await repository.recent_connection_events(), list)
    assert isinstance(await repository.recent_processing_failures(), list)


async def test_reconnect_request_queries_and_atomic_close(session_factory, clean_db):
    child_id = str(uuid.uuid4())
    guardian_id = str(uuid.uuid4())
    connection_id = str(uuid.uuid4())
    request_id = str(uuid.uuid4())
    async with session_factory() as session:
        await session.execute(text("INSERT INTO users(id, phone_number, first_name, onboarding_status) VALUES (:id, :phone, 'Auria', 'active')"), {"id": child_id, "phone": "972500000010"})
        await session.execute(text("INSERT INTO users(id, phone_number, onboarding_status) VALUES (:id, :phone, 'active')"), {"id": guardian_id, "phone": "972500000011"})
        await session.execute(text("INSERT INTO whatsapp_connections(id, user_id, provider, provider_connection_id, credentials, webhook_token_hash, connection_status) VALUES (:id, :user_id, 'baileys', :provider_id, :credentials, :token, 'pairing_required')"), {"id": connection_id, "user_id": child_id, "provider_id": str(uuid.uuid4()), "credentials": b"", "token": b""})
        await session.execute(text("INSERT INTO guardian_child_links(guardian_user_id, child_user_id, status, guardian_authorized_at, safety_enabled_at) VALUES (:guardian, :child, 'active', now(), now())"), {"guardian": guardian_id, "child": child_id})
        await session.execute(text("INSERT INTO guard_reconnect_requests(id, child_user_id, guardian_user_id, episode_key, connection_id) VALUES (:id, :child, :guardian, 'episode-queries', :connection)"), {"id": request_id, "child": child_id, "guardian": guardian_id, "connection": connection_id})
        await session.commit()
    repository = BaileysEventRepository(session_factory)
    assert str((await repository.list_open_reconnect_requests(guardian_id))[0]["id"]) == request_id
    assert str((await repository.list_open_reconnect_requests_for_child(child_id))[0]["guardian_user_id"]) == guardian_id
    loaded = await repository.get_reconnect_request(request_id)
    assert loaded is not None and loaded["first_name"] == "Auria"
    await repository.close_reconnect_episode(child_id, "episode-queries")
    assert await repository.list_open_reconnect_requests(guardian_id) == []
    await repository.close_open_reconnect_requests(child_id)
    await repository.complete_verified_reconnect(child_id)
    assert await repository.list_open_reconnect_requests(guardian_id) == []


async def test_wrong_account_keeps_reconnect_episode_until_verified_recovery(
    session_factory, clean_db
):
    await _create_connector_tables(session_factory)
    child_id, guardian_id, guardian2_id, connection_id, connector_id = (
        str(uuid.uuid4()) for _ in range(5)
    )
    child_phone = "+972546610654"
    guardian_phone = "+972546610653"
    guardian2_phone = "+972546610652"
    async with session_factory() as session:
        await session.execute(
            text("INSERT INTO users(id, phone_number, onboarding_status) VALUES (:id, :phone, 'active')"),
            {"id": child_id, "phone": child_phone},
        )
        await session.execute(
            text("INSERT INTO users(id, phone_number, onboarding_status) VALUES (:id, :phone, 'active')"),
            {"id": guardian_id, "phone": guardian_phone},
        )
        await session.execute(
            text("INSERT INTO users(id, phone_number, onboarding_status) VALUES (:id, :phone, 'active')"),
            {"id": guardian2_id, "phone": guardian2_phone},
        )
        await session.execute(
            text("""INSERT INTO guardian_child_links(
                guardian_user_id, child_user_id, status, guardian_authorized_at, safety_enabled_at
            ) VALUES (:guardian, :child, 'active', now(), now())"""),
            {"guardian": guardian_id, "child": child_id},
        )
        await session.execute(
            text("""INSERT INTO guardian_child_links(
                guardian_user_id, child_user_id, status, guardian_authorized_at, safety_enabled_at
            ) VALUES (:guardian, :child, 'active', now(), now())"""),
            {"guardian": guardian2_id, "child": child_id},
        )
        await session.execute(
            text("""INSERT INTO whatsapp_connections(
                id, user_id, provider, provider_connection_id, credentials,
                webhook_token_hash, connection_status
            ) VALUES (:id, :user_id, 'baileys', :provider_id, :credentials,
                      :webhook_hash, 'connected')"""),
            {"id": connection_id, "user_id": child_id, "provider_id": connector_id,
             "credentials": b"", "webhook_hash": b""},
        )
        await session.execute(
            text("INSERT INTO whatsapp_connector.connections(id, status) VALUES (:id, 'connected')"),
            {"id": connector_id},
        )
        await session.commit()

    repository = BaileysEventRepository(session_factory)

    async def apply_state(version: int, status: str, raw: str) -> bool:
        event_id = f"state:{connector_id}:{version}"
        payload = {
            "schema_version": 1, "event_type": "connection_state", "event_id": event_id,
            "provider": "baileys", "connection_id": connector_id, "status": status,
            "provider_raw_status": raw, "state_version": version,
            "timestamp": "2026-10-20T00:00:00Z",
        }
        async with session_factory() as session:
            await session.execute(
                text("""INSERT INTO whatsapp_connector.event_inbox(
                    event_id, connection_id, event_type, payload
                ) VALUES (:event_id, :connection_id, 'connection_state', CAST(:payload AS jsonb))"""),
                {"event_id": event_id, "connection_id": connector_id,
                 "payload": json.dumps(payload)},
            )
            await session.commit()
        claimed = await repository.claim_batch("worker", batch_size=100, lease_seconds=60)
        matching = [item for item in claimed if item.event.event_id == event_id]
        assert len(matching) == 1
        return await repository.process_claimed_event(matching[0], notification_message="reauth")

    async def episode_state() -> tuple[str | None, int, int]:
        async with session_factory() as session:
            episode = (await session.execute(
                text("SELECT auth_loss_episode_key FROM whatsapp_connections WHERE id=:id"),
                {"id": connection_id},
            )).scalar_one()
            open_requests = (await session.execute(
                text("SELECT count(*) FROM guard_reconnect_requests WHERE child_user_id=:child AND status='open'"),
                {"child": child_id},
            )).scalar_one()
            actions = (await session.execute(
                text("SELECT count(*) FROM scheduled_actions WHERE user_id=:guardian"),
                {"guardian": guardian_id},
            )).scalar_one()
            return episode, open_requests, actions

    assert await apply_state(1, "pairing_required", "logged_out:401") is True
    episode, open_count, action_count = await episode_state()
    assert episode is not None and (open_count, action_count) == (2, 1)
    links = PostgresGuardianChildLinkRepository(session_factory)
    assert await links.get_active_for_child(child_id) is None
    assert await apply_state(2, "connected", "open") is False
    assert await links.get_active_for_child(child_id) is None

    bot, provisioner = AsyncMock(), AsyncMock()
    provisioner.get_status.return_value = ConnectionStatusSnapshot(
        status=ConnectionStatus.PAIRING_REQUIRED, phone_number=child_phone
    )
    onboarding = OnboardingService(
        bot=bot, user_repo=PostgresUserRepository(session_factory),
        connection_repo=PostgresWhatsAppConnectionRepository(session_factory),
        provisioner=provisioner, webhook_base_url="https://echo.example.com",
        guardian_child_links=PostgresGuardianChildLinkRepository(session_factory),
        reconnect_requests=repository,
    )
    await onboarding.handle_connection_established(child_id, child_phone)
    assert bot.send_text.call_count == 1
    assert "לא תואם" in bot.send_text.call_args.args[1]
    assert await episode_state() == (episode, 2, 1)
    provisioner.get_status.return_value = ConnectionStatusSnapshot(
        status=ConnectionStatus.CONNECTED, phone_number="+972546610655"
    )
    await onboarding.handle_connection_established(child_id, child_phone)
    assert bot.send_text.call_count == 2
    assert "לא תואם" in bot.send_text.call_args.args[1]
    assert await episode_state() == (episode, 2, 1)
    assert await links.get_active_for_child(child_id) is None
    assert await apply_state(3, "pairing_required", "logged_out:401") is False
    assert await episode_state() == (episode, 2, 1)

    assert await apply_state(4, "connected", "open") is False
    async with session_factory() as session:
        await session.execute(
            text("UPDATE guardian_child_links SET status='revoked' WHERE guardian_user_id=:guardian"),
            {"guardian": guardian_id},
        )
        await session.commit()
    provisioner.get_status.return_value = ConnectionStatusSnapshot(
        status=ConnectionStatus.CONNECTED, phone_number=child_phone
    )
    await onboarding.handle_connection_established(child_id, child_phone)
    assert await episode_state() == (None, 0, 1)
    assert await links.get_active_for_child(child_id) is not None
    assert bot.send_text.call_count == 3
    assert bot.send_text.call_args.args[0] == guardian2_phone
    assert await apply_state(5, "pairing_required", "logged_out:401") is True
    new_episode, open_count, action_count = await episode_state()
    assert new_episode is not None and new_episode != episode
    assert (open_count, action_count) == (1, 1)


async def test_initial_verified_child_pairing_records_authenticated_boundary(
    session_factory, clean_db
):
    users = PostgresUserRepository(session_factory)
    guardian = await users.create_user("+972546610650", onboarding_status="active")
    child_phone = "+972546610651"
    child = await users.create_user(child_phone, onboarding_status="active")
    links = PostgresGuardianChildLinkRepository(session_factory)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    await links.upsert_pending(guardian_user_id=guardian, child_user_id=child, now=now)
    await links.authorize_guardian(guardian_user_id=guardian, child_user_id=child, now=now)
    connection_id = str(uuid.uuid4())
    async with session_factory() as session:
        await session.execute(
            text("""INSERT INTO whatsapp_connections(
                id, user_id, provider, provider_connection_id, credentials,
                webhook_token_hash, connection_status
            ) VALUES (:id, :child, 'baileys', :connector, :credentials, :hash, 'connected')"""),
            {"id": connection_id, "child": child, "connector": str(uuid.uuid4()),
             "credentials": b"", "hash": b""},
        )
        await session.commit()
    bot, provisioner = AsyncMock(), AsyncMock()
    provisioner.get_status.return_value = ConnectionStatusSnapshot(
        status=ConnectionStatus.CONNECTED, phone_number=child_phone
    )
    service = OnboardingService(
        bot=bot, user_repo=users,
        connection_repo=PostgresWhatsAppConnectionRepository(session_factory),
        provisioner=provisioner, webhook_base_url="https://echo.example.com",
        guardian_child_links=links, reconnect_requests=BaileysEventRepository(session_factory),
    )
    await service.handle_connection_established(child, child_phone)
    assert await links.get_active_for_child(child) is not None
    async with session_factory() as session:
        last_verified, episode = (await session.execute(
            text("""SELECT last_authenticated_at, auth_loss_episode_key
                    FROM whatsapp_connections WHERE id = :id"""),
            {"id": connection_id},
        )).one()
    assert last_verified is not None and episode is None
    bot.send_text.assert_awaited_once()


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
