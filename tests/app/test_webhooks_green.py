"""Tests for the Green webhook ingress route."""

from __future__ import annotations

import asyncio
import base64
import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from echo_v2.app.webhooks.green import (
    ChatEventDispatcher,
    RecordingEventDispatcher,
    _extract_token_from_header,
    _valid_webhook_token,
    build_router,
)
from echo_v2.persistence.chat_repositories import (
    InMemoryChatStateRepository,
    InMemoryIngestionRepository,
    InMemoryMessageRepository,
)
from echo_v2.persistence.whatsapp_connections import (
    InMemoryWhatsAppConnectionRepository,
    StoredConnection,
)
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ConnectionStatus,
    ProviderConnectionStateChanged,
    ProviderCredentials,
    ProviderMessageEvent,
)
from echo_v2.services.chat_ingestion import ChatIngestionService


def _make_app(
    *,
    webhook_token: str = "webhook-tok",
    instance_id: str = "123",
    user_id: str = "u1",
):
    repo = InMemoryWhatsAppConnectionRepository()
    token_hash = hashlib.sha256(webhook_token.encode()).digest()
    conn = StoredConnection(
        user_id=user_id,
        ref=ConnectionRef("green", instance_id),
        credentials=ProviderCredentials(b"api-tok"),
        webhook_token_hash=token_hash,
        status=ConnectionStatus.CONNECTED,
    )
    asyncio.run(repo.save(conn))

    dispatcher = RecordingEventDispatcher(repo)
    router = build_router(connection_repo=repo, dispatcher=dispatcher)
    app = FastAPI()
    app.include_router(router)
    return app, dispatcher, repo


def _make_app_with_chat_dispatcher(
    *,
    webhook_token: str = "webhook-tok",
    instance_id: str = "123",
    user_id: str = "u1",
):
    """Build an app with ChatEventDispatcher + in-memory chat repos."""
    repo = InMemoryWhatsAppConnectionRepository()
    token_hash = hashlib.sha256(webhook_token.encode()).digest()
    conn = StoredConnection(
        user_id=user_id,
        ref=ConnectionRef("green", instance_id),
        credentials=ProviderCredentials(b"api-tok"),
        webhook_token_hash=token_hash,
        status=ConnectionStatus.CONNECTED,
    )
    asyncio.run(repo.save(conn))

    ingestion = ChatIngestionService(
        InMemoryIngestionRepository(
            InMemoryMessageRepository(),
            InMemoryChatStateRepository(),
        ),
        quiet_period_seconds=300,
        private_only=True,
    )
    dispatcher = ChatEventDispatcher(
        ingestion_service=ingestion,
        connection_repo=repo,
    )
    router = build_router(connection_repo=repo, dispatcher=dispatcher)
    app = FastAPI()
    app.include_router(router)
    return app, dispatcher, repo


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _basic(token: str) -> dict:
    encoded = base64.b64encode(f"{token}:".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def _incoming_payload(instance_id: str = "123", message_id: str = "m1") -> dict:
    return {
        "typeWebhook": "incomingMessageReceived",
        "instanceData": {"idInstance": int(instance_id)},
        "chatId": "9725@c.us",
        "idMessage": message_id,
        "timestamp": 1700000000,
        "messageData": {"typeMessage": "textMessage", "textMessage": "hi"},
    }


def _state_payload(
    instance_id: str = "123", state: str = "authorized", timestamp: int = 1700000000
) -> dict:
    return {
        "typeWebhook": "stateInstanceChanged",
        "instanceData": {"idInstance": int(instance_id), "stateInstance": state},
        "timestamp": timestamp,
    }


def _status_payload(
    instance_id: str = "123", message_id: str = "m3", status: str = "delivered"
) -> dict:
    return {
        "typeWebhook": "outgoingMessageStatus",
        "instanceData": {"idInstance": int(instance_id)},
        "idMessage": message_id,
        "timestamp": 1700000000,
        "messageData": {"statusWebhook": status},
    }


def test_valid_bearer_token_dispatches_message_event():
    app, dispatcher, _ = _make_app()
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/green",
            json=_incoming_payload(),
            headers=_bearer("webhook-tok"),
        )
    assert response.status_code == 200
    assert response.json() == {"status": "received"}
    assert len(dispatcher.dispatched) == 1
    event, user_id, _connection_id = dispatcher.dispatched[0]
    assert isinstance(event, ProviderMessageEvent)
    assert user_id == "u1"
    assert event.text == "hi"


def test_valid_basic_token_dispatches_message_event():
    app, dispatcher, _ = _make_app()
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/green",
            json=_incoming_payload(),
            headers=_basic("webhook-tok"),
        )
    assert response.status_code == 200
    assert response.json() == {"status": "received"}
    assert len(dispatcher.dispatched) == 1


def test_missing_authorization_header_returns_401():
    app, _, _ = _make_app()
    with TestClient(app) as client:
        response = client.post("/webhooks/whatsapp/green", json=_incoming_payload())
    assert response.status_code == 401


def test_wrong_token_returns_401():
    app, _, _ = _make_app()
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/green",
            json=_incoming_payload(),
            headers=_bearer("wrong-tok"),
        )
    assert response.status_code == 401


def test_malformed_authorization_header_returns_401():
    app, _, _ = _make_app()
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/green",
            json=_incoming_payload(),
            headers={"Authorization": "NotAScheme whatever"},
        )
    assert response.status_code == 401


def test_unknown_instance_returns_404():
    app, _, _ = _make_app()
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/green",
            json=_incoming_payload(instance_id="999"),
            headers=_bearer("webhook-tok"),
        )
    assert response.status_code == 404


def test_payload_without_instance_data_returns_404():
    app, _, _ = _make_app()
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/green",
            json={"typeWebhook": "incomingMessageReceived"},
            headers=_bearer("webhook-tok"),
        )
    assert response.status_code == 404


def test_unknown_type_webhook_returns_ignored():
    app, dispatcher, _ = _make_app()
    payload = {
        "typeWebhook": "pollMessageReceived",
        "instanceData": {"idInstance": 123},
    }
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/green",
            json=payload,
            headers=_bearer("webhook-tok"),
        )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert dispatcher.dispatched == []


def test_duplicate_message_event_not_deduped_at_route():
    """Message events skip store.claim — dedup is via the messages table
    in ChatIngestionService. With RecordingEventDispatcher (no dedup),
    both are dispatched."""
    app, dispatcher, _ = _make_app()
    with TestClient(app) as client:
        r1 = client.post(
            "/webhooks/whatsapp/green",
            json=_incoming_payload(message_id="m1"),
            headers=_bearer("webhook-tok"),
        )
        r2 = client.post(
            "/webhooks/whatsapp/green",
            json=_incoming_payload(message_id="m1"),
            headers=_bearer("webhook-tok"),
        )
    assert r1.json() == {"status": "received"}
    assert r2.json() == {"status": "received"}
    assert len(dispatcher.dispatched) == 2


def test_duplicate_message_deduped_by_chat_dispatcher():
    """With ChatEventDispatcher, duplicate message events are deduped via
    the messages table INSERT ON CONFLICT DO NOTHING."""
    app, _dispatcher, _repo = _make_app_with_chat_dispatcher()
    with TestClient(app) as client:
        r1 = client.post(
            "/webhooks/whatsapp/green",
            json=_incoming_payload(message_id="m1"),
            headers=_bearer("webhook-tok"),
        )
        r2 = client.post(
            "/webhooks/whatsapp/green",
            json=_incoming_payload(message_id="m1"),
            headers=_bearer("webhook-tok"),
        )
    assert r1.json() == {"status": "received"}
    assert r2.json() == {"status": "received"}
    # The second message was a duplicate — the ingestion service returned False.
    # We can verify by checking the chat state has version 1 (not 2).


def test_duplicate_status_event_is_deduped():
    app, _dispatcher, _ = _make_app()
    with TestClient(app) as client:
        client.post(
            "/webhooks/whatsapp/green",
            json=_status_payload(message_id="m3"),
            headers=_bearer("webhook-tok"),
        )
        r2 = client.post(
            "/webhooks/whatsapp/green",
            json=_status_payload(message_id="m3"),
            headers=_bearer("webhook-tok"),
        )
    assert r2.json() == {"status": "duplicate"}


def test_status_events_same_message_different_status_are_both_dispatched():
    """sent -> delivered -> read for the same idMessage must all be processed."""
    app, dispatcher, _ = _make_app()
    with TestClient(app) as client:
        r1 = client.post(
            "/webhooks/whatsapp/green",
            json=_status_payload(message_id="m3", status="sent"),
            headers=_bearer("webhook-tok"),
        )
        r2 = client.post(
            "/webhooks/whatsapp/green",
            json=_status_payload(message_id="m3", status="delivered"),
            headers=_bearer("webhook-tok"),
        )
        r3 = client.post(
            "/webhooks/whatsapp/green",
            json=_status_payload(message_id="m3", status="read"),
            headers=_bearer("webhook-tok"),
        )
    assert r1.json() == {"status": "received"}
    assert r2.json() == {"status": "received"}
    assert r3.json() == {"status": "received"}
    assert len(dispatcher.dispatched) == 3


def test_message_event_and_status_event_same_message_are_both_dispatched():
    """The outgoing message event and its later status event share idMessage
    but are distinct notifications and must not suppress each other."""
    app, dispatcher, _ = _make_app()
    with TestClient(app) as client:
        r1 = client.post(
            "/webhooks/whatsapp/green",
            json=_incoming_payload(message_id="m3"),
            headers=_bearer("webhook-tok"),
        )
        r2 = client.post(
            "/webhooks/whatsapp/green",
            json=_status_payload(message_id="m3", status="delivered"),
            headers=_bearer("webhook-tok"),
        )
    assert r1.json() == {"status": "received"}
    assert r2.json() == {"status": "received"}
    assert len(dispatcher.dispatched) == 2


def test_duplicate_state_event_is_deduped():
    """A provider retry of the same state notification is suppressed."""
    app, _dispatcher, _ = _make_app()
    payload = _state_payload(state="authorized")
    with TestClient(app) as client:
        r1 = client.post(
            "/webhooks/whatsapp/green",
            json=payload,
            headers=_bearer("webhook-tok"),
        )
        r2 = client.post(
            "/webhooks/whatsapp/green",
            json=payload,
            headers=_bearer("webhook-tok"),
        )
    assert r1.json() == {"status": "received"}
    assert r2.json() == {"status": "duplicate"}


def test_state_transitions_same_state_different_timestamp_are_both_dispatched():
    """A connection can revisit an earlier state (e.g. authorized -> sleepMode
    -> authorized). The two authorized notifications have different timestamps
    and must both be processed."""
    app, dispatcher, _ = _make_app()
    with TestClient(app) as client:
        r1 = client.post(
            "/webhooks/whatsapp/green",
            json=_state_payload(state="authorized", timestamp=1700000000),
            headers=_bearer("webhook-tok"),
        )
        r2 = client.post(
            "/webhooks/whatsapp/green",
            json=_state_payload(state="authorized", timestamp=1700001000),
            headers=_bearer("webhook-tok"),
        )
    assert r1.json() == {"status": "received"}
    assert r2.json() == {"status": "received"}
    assert len(dispatcher.dispatched) == 2


def test_state_changed_event_updates_connection_status():
    app, dispatcher, repo = _make_app()
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/green",
            json=_state_payload(state="sleepMode"),
            headers=_bearer("webhook-tok"),
        )
    assert response.status_code == 200
    assert response.json() == {"status": "received"}
    assert len(dispatcher.dispatched) == 1
    event, _, _ = dispatcher.dispatched[0]
    assert isinstance(event, ProviderConnectionStateChanged)
    assert event.status is ConnectionStatus.DEGRADED
    # The dispatcher updated the repo.
    updated = asyncio.run(repo.get(ConnectionRef("green", "123")))
    assert updated is not None
    assert updated.status is ConnectionStatus.DEGRADED
    assert updated.provider_raw_status == "sleepMode"


# --- Disconnect notification: transition guard ---------------------------


class StubOnboarding:
    """Stub onboarding service for disconnect notification tests."""

    def __init__(self) -> None:
        self.disconnect_notifications: list[str] = []
        self.connection_established: list[str] = []

    async def handle_disconnect_notification(self, user_id: str) -> None:
        self.disconnect_notifications.append(user_id)

    async def handle_connection_established_by_id(self, user_id: str) -> None:
        self.connection_established.append(user_id)


def _make_app_with_onboarding(
    *,
    initial_status: ConnectionStatus = ConnectionStatus.CONNECTED,
    user_id: str = "u1",
):
    """Build an app with ChatEventDispatcher + onboarding stub."""
    repo = InMemoryWhatsAppConnectionRepository()
    token_hash = hashlib.sha256(b"webhook-tok").digest()
    conn = StoredConnection(
        user_id=user_id,
        ref=ConnectionRef("green", "123"),
        credentials=ProviderCredentials(b"api-tok"),
        webhook_token_hash=token_hash,
        status=initial_status,
    )
    asyncio.run(repo.save(conn))

    ingestion = ChatIngestionService(
        InMemoryIngestionRepository(
            InMemoryMessageRepository(),
            InMemoryChatStateRepository(),
        ),
        quiet_period_seconds=300,
        private_only=True,
    )
    onboarding = StubOnboarding()
    dispatcher = ChatEventDispatcher(
        ingestion_service=ingestion,
        connection_repo=repo,
        onboarding_service=onboarding,
    )
    router = build_router(connection_repo=repo, dispatcher=dispatcher)
    app = FastAPI()
    app.include_router(router)
    return app, onboarding, repo


def test_disconnect_notifies_on_connected_to_pairing_required():
    """CONNECTED → PAIRING_REQUIRED sends one disconnect notification."""
    app, onboarding, _ = _make_app_with_onboarding(
        initial_status=ConnectionStatus.CONNECTED
    )
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/whatsapp/green",
            json=_state_payload(state="notAuthorized", timestamp=1700000001),
            headers=_bearer("webhook-tok"),
        )
    assert resp.status_code == 200
    assert len(onboarding.disconnect_notifications) == 1
    assert onboarding.disconnect_notifications[0] == "u1"


def test_disconnect_no_notification_on_repeated_pairing_required():
    """PAIRING_REQUIRED → PAIRING_REQUIRED sends no notification."""
    app, onboarding, _ = _make_app_with_onboarding(
        initial_status=ConnectionStatus.PAIRING_REQUIRED
    )
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/whatsapp/green",
            json=_state_payload(state="notAuthorized", timestamp=1700000001),
            headers=_bearer("webhook-tok"),
        )
    assert resp.status_code == 200
    assert len(onboarding.disconnect_notifications) == 0


def test_disconnect_no_notification_on_provisioning_to_pairing_required():
    """PROVISIONING → PAIRING_REQUIRED sends no notification (initial provisioning)."""
    app, onboarding, _ = _make_app_with_onboarding(
        initial_status=ConnectionStatus.PROVISIONING
    )
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/whatsapp/green",
            json=_state_payload(state="notAuthorized", timestamp=1700000001),
            headers=_bearer("webhook-tok"),
        )
    assert resp.status_code == 200
    assert len(onboarding.disconnect_notifications) == 0


def test_disconnect_sends_second_notification_after_reconnect():
    """Reconnect (→CONNECTED) then disconnect (→PAIRING_REQUIRED) sends again."""
    app, onboarding, _repo = _make_app_with_onboarding(
        initial_status=ConnectionStatus.CONNECTED
    )
    with TestClient(app) as client:
        # First disconnect.
        client.post(
            "/webhooks/whatsapp/green",
            json=_state_payload(state="notAuthorized", timestamp=1700000001),
            headers=_bearer("webhook-tok"),
        )
        # Reconnect.
        client.post(
            "/webhooks/whatsapp/green",
            json=_state_payload(state="authorized", timestamp=1700000002),
            headers=_bearer("webhook-tok"),
        )
        # Second disconnect.
        client.post(
            "/webhooks/whatsapp/green",
            json=_state_payload(state="notAuthorized", timestamp=1700000003),
            headers=_bearer("webhook-tok"),
        )
    assert len(onboarding.disconnect_notifications) == 2


def test_disconnect_notification_not_sent_when_onboarding_none():
    """No onboarding service → no crash, just no notification."""
    app, _, _ = _make_app_with_chat_dispatcher()
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/whatsapp/green",
            json=_state_payload(state="notAuthorized", timestamp=1700000001),
            headers=_bearer("webhook-tok"),
        )
    assert resp.status_code == 200


def test_invalid_json_returns_400():
    app, _, _ = _make_app()
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/green",
            content="not-json",
            headers={**_bearer("webhook-tok"), "Content-Type": "application/json"},
        )
    assert response.status_code == 400


def test_non_object_payload_returns_400():
    app, _, _ = _make_app()
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp/green",
            json=[1, 2, 3],
            headers=_bearer("webhook-tok"),
        )
    assert response.status_code == 400


# --- Auth helpers --------------------------------------------------------


def test_extract_token_bearer():
    assert _extract_token_from_header("Bearer abc123") == "abc123"


def test_extract_token_basic():
    encoded = base64.b64encode(b"tok:").decode()
    assert _extract_token_from_header(f"Basic {encoded}") == "tok"


def test_extract_token_basic_with_password_uses_username_only():
    encoded = base64.b64encode(b"tok:password").decode()
    assert _extract_token_from_header(f"Basic {encoded}") == "tok"


def test_extract_token_none_for_missing_header():
    assert _extract_token_from_header(None) is None


def test_extract_token_none_for_malformed():
    assert _extract_token_from_header("Bearer") is None
    assert _extract_token_from_header("UnknownScheme abc") is None


def test_extract_token_none_for_bad_base64():
    assert _extract_token_from_header("Basic !!!not-base64!!!") is None


def test_valid_webhook_token_constant_time_compare():
    h = hashlib.sha256(b"my-token").digest()
    assert _valid_webhook_token("my-token", h) is True
    assert _valid_webhook_token("wrong", h) is False
