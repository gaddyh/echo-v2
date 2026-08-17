"""Tests for the provider-neutral WhatsApp ports and types."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from echo_v2.ports.whatsapp import (
    ConnectionConfig,
    ConnectionRef,
    ConnectionStatus,
    ConnectionStatusSnapshot,
    CreatedConnection,
    CredentialResolver,
    MessageDirection,
    MessageKind,
    MessageSource,
    PairingOutcome,
    PairingQr,
    PairingResult,
    ProviderCredentials,
    ProviderMessageEvent,
    WhatsAppEventAdapter,
    WhatsAppEventSubscription,
    WhatsAppMessaging,
    WhatsAppProvisioner,
)


def test_connection_status_covers_all_neutral_states():
    expected = {
        "PROVISIONING",
        "CONNECTING",
        "PAIRING_REQUIRED",
        "CONNECTED",
        "DEGRADED",
        "BLOCKED",
        "SUSPENDED",
        "UNKNOWN",
    }
    assert {s.name for s in ConnectionStatus} == expected


def test_connection_ref_is_frozen():
    ref = ConnectionRef(provider="green", provider_connection_id="123")
    with pytest.raises(FrozenInstanceError):
        ref.provider = "meta"  # type: ignore[misc]


def test_provider_credentials_repr_does_not_leak_secret():
    creds = ProviderCredentials(data=b"super-secret-token")
    assert "super-secret-token" not in repr(creds)
    assert "redacted" in repr(creds)


def test_provider_credentials_is_frozen():
    creds = ProviderCredentials(data=b"x")
    with pytest.raises(FrozenInstanceError):
        creds.data = b"y"  # type: ignore[misc]


def test_created_connection_pairs_ref_and_credentials():
    ref = ConnectionRef("green", "123")
    creds = ProviderCredentials(b"tok")
    created = CreatedConnection(ref=ref, credentials=creds)
    assert created.ref is ref
    assert created.credentials is creds


def test_subscription_defaults_all_true():
    sub = WhatsAppEventSubscription()
    assert (
        sub.incoming_messages
        and sub.outgoing_phone_messages
        and sub.outgoing_api_messages
        and sub.message_statuses
        and sub.connection_state
    )


def test_connection_config_requires_webhook_url_and_token():
    config = ConnectionConfig(webhook_url="https://echo/hook", webhook_token="tok")
    assert config.webhook_url == "https://echo/hook"
    assert config.webhook_token == "tok"
    assert isinstance(config.subscriptions, WhatsAppEventSubscription)


def test_connection_status_snapshot_defaults_checked_at_to_utc_now():
    from datetime import datetime, timezone

    before = datetime.now(timezone.utc)
    snap = ConnectionStatusSnapshot(status=ConnectionStatus.CONNECTED)
    after = datetime.now(timezone.utc)
    assert snap.provider_raw_status is None
    assert before <= snap.checked_at <= after
    assert snap.checked_at.tzinfo is timezone.utc


def test_pairing_result_qr_ready():
    qr = PairingQr(image_base64="b64", expires_at=None)
    result = PairingResult(outcome=PairingOutcome.QR_READY, qr=qr)
    assert result.outcome is PairingOutcome.QR_READY
    assert result.qr is qr
    assert result.message is None


def test_message_source_does_not_use_phone():
    # G7: USER (not PHONE) -- covers desktop/linked devices too.
    assert not hasattr(MessageSource, "PHONE")
    assert MessageSource.USER.name == "USER"
    assert MessageSource.API.name == "API"


def test_message_kind_neutral_enum_present():
    kinds = {k.name for k in MessageKind}
    assert {"TEXT", "IMAGE", "AUDIO", "VIDEO", "DOCUMENT", "REACTION", "OTHER"} <= kinds


def test_message_direction_values():
    assert MessageDirection.INBOUND.value == "inbound"
    assert MessageDirection.OUTBOUND.value == "outbound"


def test_provider_message_event_has_no_raw_field():
    import dataclasses

    fields = {f.name for f in dataclasses.fields(ProviderMessageEvent)}
    assert "raw" not in fields
    assert "user_id" not in fields
    assert "connection" in fields


def test_protocols_are_runtime_checkable():
    # Structural checks against dummy implementations.
    class _Prov:
        async def create_connection(self, config): ...
        async def configure_connection(self, connection, config): ...
        async def get_status(self, connection): ...
        async def get_pairing_qr(self, connection): ...
        async def unpair(self, connection): ...
        async def delete_connection(self, connection): ...

    class _Adapter:
        def parse(self, payload): ...

    class _Resolver:
        async def get_credentials(self, ref): ...

    class _Messaging:
        async def send_message(self, connection, chat_id, message): ...

    assert isinstance(_Prov(), WhatsAppProvisioner)
    assert isinstance(_Adapter(), WhatsAppEventAdapter)
    assert isinstance(_Resolver(), CredentialResolver)
    assert isinstance(_Messaging(), WhatsAppMessaging)
