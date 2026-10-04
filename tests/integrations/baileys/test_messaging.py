from __future__ import annotations

from echo_v2.integrations.baileys.messaging import BaileysMessaging
from echo_v2.ports.whatsapp import ConnectionRef, WhatsAppMessaging


class FakeBaileysClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def send_message(self, connection_id: str, chat_id: str, message: str) -> str:
        self.calls.append((connection_id, chat_id, message))
        return "baileys-msg-1"


def test_baileys_messaging_satisfies_port_protocol() -> None:
    assert isinstance(BaileysMessaging.__new__(BaileysMessaging), WhatsAppMessaging)


async def test_send_message_delegates_provider_connection_id() -> None:
    client = FakeBaileysClient()
    messaging = BaileysMessaging(client)  # type: ignore[arg-type]

    result = await messaging.send_message(
        ConnectionRef("baileys", "connection-1"),
        "15551234567@s.whatsapp.net",
        "hello",
    )

    assert result == "baileys-msg-1"
    assert client.calls == [
        ("connection-1", "15551234567@s.whatsapp.net", "hello")
    ]


async def test_send_message_translates_green_phone_jid() -> None:
    client = FakeBaileysClient()
    messaging = BaileysMessaging(client)  # type: ignore[arg-type]

    await messaging.send_message(
        ConnectionRef("baileys", "connection-1"),
        "15551234567@c.us",
        "hello",
    )

    assert client.calls == [
        ("connection-1", "15551234567@s.whatsapp.net", "hello")
    ]
