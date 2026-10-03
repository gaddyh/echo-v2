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
