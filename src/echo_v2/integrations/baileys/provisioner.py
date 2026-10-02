"""Baileys implementation of the provider-neutral WhatsApp provisioner."""

from __future__ import annotations

from echo_v2.integrations.baileys.client import BaileysClient
from echo_v2.ports.whatsapp import (
    ConnectionConfig,
    ConnectionRef,
    ConnectionStatus,
    ConnectionStatusSnapshot,
    CreatedConnection,
    PairingOutcome,
    PairingQr,
    PairingResult,
    ProviderCredentials,
)

__all__ = ["BaileysProvisioner"]


class BaileysProvisioner:
    """Translate Echo lifecycle calls into connector HTTP calls.

    Baileys auth state and credentials are connector-owned. Echo only stores
    the opaque connector connection id and an explicit empty credential payload
    required by the current persistence model.
    """

    def __init__(self, client: BaileysClient) -> None:
        self._client = client

    async def create_connection(self, config: ConnectionConfig) -> CreatedConnection:
        del config
        data = await self._client.create_connection()
        connection_id = data.get("connection_id")
        if not connection_id:
            raise ValueError("Baileys connector response missing connection_id")
        return CreatedConnection(
            ref=ConnectionRef(provider="baileys", provider_connection_id=str(connection_id)),
            credentials=ProviderCredentials(data=b""),
        )

    async def configure_connection(
        self,
        connection: ConnectionRef,
        config: ConnectionConfig,
    ) -> None:
        """No-op: Baileys owns its socket and does not use provider webhooks."""
        del connection, config

    async def get_status(self, connection: ConnectionRef) -> ConnectionStatusSnapshot:
        data = await self._client.get_status(connection.provider_connection_id)
        raw_status = data.get("provider_raw_status") or data.get("status")
        status = _status(data.get("status"))
        return ConnectionStatusSnapshot(
            status=status,
            provider_raw_status=str(raw_status) if raw_status is not None else None,
        )

    async def get_pairing_qr(self, connection: ConnectionRef) -> PairingResult:
        data = await self._client.get_qr(connection.provider_connection_id)
        outcome = data.get("outcome")
        if outcome == "qr_ready":
            image = data.get("image_base64")
            if not isinstance(image, str) or not image:
                return PairingResult(
                    outcome=PairingOutcome.TIMEOUT,
                    message="connector returned an empty QR",
                )
            return PairingResult(
                outcome=PairingOutcome.QR_READY,
                qr=PairingQr(image_base64=image),
            )
        if outcome == "already_authorized":
            return PairingResult(outcome=PairingOutcome.ALREADY_AUTHORIZED)
        return PairingResult(
            outcome=PairingOutcome.TIMEOUT,
            message=str(data.get("message") or "pairing QR unavailable"),
        )

    async def unpair(self, connection: ConnectionRef) -> None:
        await self._client.unpair(connection.provider_connection_id)

    async def delete_connection(self, connection: ConnectionRef) -> None:
        await self._client.delete_connection(connection.provider_connection_id)


def _status(raw: object) -> ConnectionStatus:
    if isinstance(raw, str):
        try:
            return ConnectionStatus(raw)
        except ValueError:
            pass
    return ConnectionStatus.UNKNOWN
