from __future__ import annotations

from datetime import datetime, timezone

import pytest

from echo_v2.integrations.baileys.events import BaileysConnectionStateEvent
from echo_v2.persistence.baileys_events import (
    _is_real_auth_loss,
    reauth_action_id,
    reauth_notification_key,
)
from echo_v2.ports.whatsapp import ConnectionStatus


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "event_type": "connection_state",
        "event_id": "state:connection:42",
        "provider": "baileys",
        "connection_id": "connection",
        "status": "pairing_required",
        "provider_raw_status": "logged_out:401",
        "state_version": 42,
        "timestamp": "2026-10-03T00:00:00Z",
    }
    payload.update(overrides)
    return payload


def test_state_parser_rejects_unsupported_version_and_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="schema version"):
        BaileysConnectionStateEvent.from_payload(_payload(schema_version=2), inbox_id=7)
    with pytest.raises(ValueError, match="timezone"):
        BaileysConnectionStateEvent.from_payload(
            _payload(timestamp="2026-10-03T00:00:00"), inbox_id=7
        )


def test_parse_connection_state_event() -> None:
    event = BaileysConnectionStateEvent.from_payload(_payload(), inbox_id=7)

    assert event.event_id == "state:connection:42"
    assert event.inbox_id == 7
    assert event.status is ConnectionStatus.PAIRING_REQUIRED
    assert event.state_version == 42
    assert event.timestamp == datetime(2026, 10, 3, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw", ["logged_out:401", "device_removed:401"])
def test_real_auth_loss(raw: str) -> None:
    assert _is_real_auth_loss(raw) is True


@pytest.mark.parametrize(
    "raw",
    [None, "operator_unpair", "disconnect:408", "disconnect:428", "disconnect:515"],
)
def test_non_auth_loss(raw: str | None) -> None:
    assert _is_real_auth_loss(raw) is False


def test_reauth_action_identity_is_deterministic() -> None:
    assert reauth_notification_key("state:connection:42") == (
        "baileys-reauth:state:connection:42"
    )
    assert reauth_action_id("state:connection:42") == reauth_action_id(
        "state:connection:42"
    )
    assert reauth_action_id("state:connection:42") != reauth_action_id(
        "state:connection:43"
    )


def test_rejects_wrong_event_type_and_provider() -> None:
    with pytest.raises(ValueError, match="event type"):
        BaileysConnectionStateEvent.from_payload(
            _payload(event_type="message"), inbox_id=7
        )
    with pytest.raises(ValueError, match="provider"):
        BaileysConnectionStateEvent.from_payload(
            _payload(provider="green"), inbox_id=7
        )


@pytest.mark.parametrize(
    "field",
    ["event_id", "connection_id", "status", "timestamp"],
)
def test_rejects_missing_required_fields(field: str) -> None:
    payload = _payload()
    payload.pop(field)
    with pytest.raises((TypeError, ValueError)):
        BaileysConnectionStateEvent.from_payload(payload, inbox_id=7)


def test_rejects_invalid_raw_status_and_version() -> None:
    with pytest.raises(ValueError, match="provider_raw_status"):
        BaileysConnectionStateEvent.from_payload(
            _payload(provider_raw_status=401), inbox_id=7
        )
    with pytest.raises(ValueError, match="state_version"):
        BaileysConnectionStateEvent.from_payload(
            _payload(state_version="42"), inbox_id=7
        )


def test_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="unknown Baileys connection status"):
        BaileysConnectionStateEvent.from_payload(
            _payload(status="not_authorized"), inbox_id=7
        )


def _message_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "event_type": "message",
        "event_id": "message:connection:msg:in",
        "provider": "baileys",
        "connection_id": "connection",
        "chat_id": "15551234567@s.whatsapp.net",
        "is_group": False,
        "provider_message_id": "msg",
        "direction": "inbound",
        "source": None,
        "timestamp": "2026-10-03T00:00:00Z",
        "kind": "audio",
        "text": None,
        "sender": {"canonical_id": "15551234567@s.whatsapp.net", "display_name": "Alice"},
        "media_reference": "message:msg",
        "media_download_url": "https://connector.test/media/connection/message%3Amsg?expires=1&signature=x",
        "media_mime_type": "audio/ogg",
        "media_file_name": "voice.ogg",
    }
    payload.update(overrides)
    return payload


def test_parse_versioned_message_event() -> None:
    from echo_v2.integrations.baileys.events import BaileysMessageEvent
    from echo_v2.ports.whatsapp import MessageDirection, MessageKind

    event = BaileysMessageEvent.from_payload(_message_payload())

    assert event.connection.provider == "baileys"
    assert event.direction is MessageDirection.INBOUND
    assert event.kind is MessageKind.AUDIO
    assert event.is_group is False
    assert event.sender_id == "15551234567@s.whatsapp.net"
    assert event.media_reference == "message:msg"
    assert event.media_download_url is not None


def test_parse_preserves_api_outbound_source() -> None:
    from echo_v2.integrations.baileys.events import BaileysMessageEvent
    from echo_v2.ports.whatsapp import MessageDirection, MessageSource

    event = BaileysMessageEvent.from_payload(
        _message_payload(direction="outbound", source="api", kind="text", text="sent")
    )

    assert event.direction is MessageDirection.OUTBOUND
    assert event.source is MessageSource.API
    assert event.text == "sent"


@pytest.mark.parametrize("schema_version", [None, 2])
def test_message_parser_rejects_unsupported_schema(schema_version: object) -> None:
    from echo_v2.integrations.baileys.events import BaileysMessageEvent

    with pytest.raises(ValueError, match="schema version"):
        BaileysMessageEvent.from_payload(
            _message_payload(schema_version=schema_version)
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("event_type", "connection_state"), ("provider", "green")],
)
def test_message_parser_rejects_wrong_envelope(field: str, value: object) -> None:
    from echo_v2.integrations.baileys.events import BaileysMessageEvent

    with pytest.raises(ValueError):
        BaileysMessageEvent.from_payload(_message_payload(**{field: value}))


def test_message_parser_rejects_invalid_optional_metadata() -> None:
    from echo_v2.integrations.baileys.events import BaileysMessageEvent

    for field in ("text", "chat_name", "media_reference", "media_download_url", "media_mime_type", "media_file_name"):
        with pytest.raises(ValueError, match=field):
            BaileysMessageEvent.from_payload(_message_payload(**{field: 1}))


def test_message_parser_rejects_invalid_required_metadata() -> None:
    from echo_v2.integrations.baileys.events import BaileysMessageEvent

    for field, value in (
        ("event_id", ""),
        ("connection_id", None),
        ("chat_id", 1),
        ("provider_message_id", ""),
        ("timestamp", "not-a-timestamp"),
        ("direction", "sideways"),
        ("source", "unknown"),
        ("kind", "unknown"),
        ("is_group", None),
        ("sender", "invalid"),
    ):
        with pytest.raises((TypeError, ValueError)):
            BaileysMessageEvent.from_payload(_message_payload(**{field: value}))
