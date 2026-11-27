"""Route WhatsApp provisioner calls between the default provider and WAHA.

Provider selection for **new** connections is a single global switch
(``WAHA_ONBOARDING_ENABLED``, default off): when disabled, all behavior is
exactly the pre-WAHA production behavior. When enabled, new onboardings
provision via WAHA.

Everything else routes on ``ref.provider`` — existing connections always
keep their provider, so Baileys/Green users are unaffected by the switch and
existing WAHA connections remain fully manageable (status, on-demand QR,
reconciliation) even when new WAHA onboarding is disabled.
"""

from __future__ import annotations

import logging

from echo_v2.integrations.waha.provisioner import WahaProvisioner
from echo_v2.ports.whatsapp import (
    ConnectionConfig,
    ConnectionRef,
    ConnectionStatusSnapshot,
    CreatedConnection,
    PairingResult,
    WhatsAppProvisioner,
)

__all__ = ["ProvisionerRouter"]

_logger = logging.getLogger("echo_v2.services.provisioner_router")

_PROVIDER_WAHA = "waha"


class ProvisionerRouter:
    """A :class:`WhatsAppProvisioner` that picks the provider per call."""

    def __init__(
        self,
        *,
        default: WhatsAppProvisioner,
        waha: WahaProvisioner,
        waha_onboarding_enabled: bool,
    ) -> None:
        self._default = default
        self._waha = waha
        self._waha_onboarding_enabled = waha_onboarding_enabled

    async def create_connection(self, config: ConnectionConfig) -> CreatedConnection:
        if self._waha_onboarding_enabled:
            return await self._waha.create_connection(config)
        return await self._default.create_connection(config)

    async def configure_connection(
        self,
        connection: ConnectionRef,
        config: ConnectionConfig,
    ) -> None:
        return await self._route(connection).configure_connection(connection, config)

    async def get_status(self, connection: ConnectionRef) -> ConnectionStatusSnapshot:
        return await self._route(connection).get_status(connection)

    async def get_pairing_qr(self, connection: ConnectionRef) -> PairingResult:
        return await self._route(connection).get_pairing_qr(connection)

    async def unpair(self, connection: ConnectionRef) -> None:
        await self._route(connection).unpair(connection)

    async def delete_connection(self, connection: ConnectionRef) -> None:
        await self._route(connection).delete_connection(connection)

    def _route(self, connection: ConnectionRef) -> WhatsAppProvisioner:
        """Route by the connection's provider — never by the onboarding flag.

        Existing WAHA connections keep working through the WAHA provisioner
        even when new WAHA onboarding is disabled; everything else goes to
        the default provider exactly as before.
        """
        if connection.provider == _PROVIDER_WAHA:
            return self._waha
        return self._default
