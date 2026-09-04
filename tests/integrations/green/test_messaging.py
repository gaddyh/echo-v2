"""Tests for GreenMessaging against a fake GreenClient + fake resolver."""

from __future__ import annotations

import pytest

from echo_v2.integrations.green.messaging import GreenMessaging
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    CredentialResolver,
    ProviderCredentials,
    WhatsAppMessaging,
)
from echo_v2.runtime.errors import PermanentError


class FakeGreenClient:
    """Records calls and returns canned responses for send_message."""

    def __init__(self, *, send_response_id: str = "MSG_999") -> None:
        self.send_response_id = send_response_id
        self.calls: list[tuple[str, tuple, dict]] = []

    async def send_message(self, id_instance, api_token, chat_id, message):
        self.calls.append(
            ("send_message", (id_instance, api_token), {"chat_id": chat_id, "message": message})
        )
        return self.send_response_id


class FakeResolver:
    def __init__(self, credentials: ProviderCredentials | None) -> None:
        self._credentials = credentials

    async def get_credentials(self, ref: ConnectionRef) -> ProviderCredentials | None:
        return self._credentials


def test_green_messaging_satisfies_port_protocol():
    msg = GreenMessaging.__new__(GreenMessaging)
    assert isinstance(msg, WhatsAppMessaging)


async def test_send_message_resolves_credentials_and_calls_client():
    client = FakeGreenClient()
    msg = GreenMessaging(client, FakeResolver(ProviderCredentials(b"api-tok")))
    msg_id = await msg.send_message(ConnectionRef("green", "123"), "972@c.us", "hello")
    assert msg_id == "MSG_999"
    name, (id_instance, api_token), kwargs = client.calls[0]
    assert name == "send_message"
    assert id_instance == "123"
    assert api_token == "api-tok"
    assert kwargs == {"chat_id": "972@c.us", "message": "hello"}


async def test_send_message_missing_credentials_raises_permanent_error():
    client = FakeGreenClient()
    msg = GreenMessaging(client, FakeResolver(None))
    with pytest.raises(PermanentError):
        await msg.send_message(ConnectionRef("green", "123"), "972@c.us", "hello")
    assert client.calls == []


def test_fake_resolver_satisfies_credential_resolver_protocol():
    assert isinstance(FakeResolver(None), CredentialResolver)
