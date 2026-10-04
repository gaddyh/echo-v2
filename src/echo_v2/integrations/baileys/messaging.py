"""Baileys implementation of the provider-neutral WhatsApp messaging port."""

from __future__ import annotations

from echo_v2.integrations.baileys.client import BaileysClient
from echo_v2.ports.whatsapp import ConnectionRef

__all__ = ["BaileysMessaging"]


def _to_baileys_chat_id(chat_id: str) -> str:
    """Translate the Green-compatible phone JID to Baileys' phone JID."""
    if chat_id.endswith("@c.us"):
        return f"{chat_id.removesuffix('@c.us')}@s.whatsapp.net"
    return chat_id


class BaileysMessaging:
    """Send WhatsApp messages through the Echo Baileys connector."""

    def __init__(self, client: BaileysClient) -> None:
        self._client = client

    async def send_message(
        self,
        connection: ConnectionRef,
        chat_id: str,
        message: str,
    ) -> str:
        """Send a text message and return the provider message id."""
        return await self._client.send_message(
            connection.provider_connection_id,
            _to_baileys_chat_id(chat_id),
            message,
        )
