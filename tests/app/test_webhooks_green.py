"""Tests for the Green webhook ingress route."""

from __future__ import annotations

import asyncio
import base64
import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from echo_v2.app.webhooks.green import (
    RecordingEventDispatcher,
    _extract_token_from_header,
    _valid_webhook_token,
    build_router,
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


def _state_payload(instance_id: str = "123", state: str = "authorized") -> dict:
    return {
        "typeWebhook": "stateInstanceChanged",
        "instanceData": {"idInstance": int(instance_id), "stateInstance": state},
        "timestamp": 1700000000,
    }


def _status_payload(instance_id: str = "123", message_id: str = "m3") -> dict:
    return {
        "typeWebhook": "outgoingMessageStatus",
        "instanceData": {"idInstance": int(instance_id)},
        "idMessage": message_id,
        "timestamp": 1700000000,
        "messageData": {"statusWebhook": "delivered"},
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
    event, user_id = dispatcher.dispatched[0]
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


def test_duplicate_message_id_is_deduped():
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
    assert r2.json() == {"status": "duplicate"}
    assert len(dispatcher.dispatched) == 1


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
    # State events are not deduped (no provider_message_id), so they always dispatch.
    assert len(dispatcher.dispatched) == 1
    event, _ = dispatcher.dispatched[0]
    assert isinstance(event, ProviderConnectionStateChanged)
    assert event.status is ConnectionStatus.DEGRADED
    # The dispatcher updated the repo.
    updated = asyncio.run(repo.get(ConnectionRef("green", "123")))
    assert updated is not None
    assert updated.status is ConnectionStatus.DEGRADED
    assert updated.provider_raw_status == "sleepMode"


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
