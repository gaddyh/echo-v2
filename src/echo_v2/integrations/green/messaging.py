"""Green implementation of :class:`WhatsAppMessaging`.

Translates provider-neutral send calls into :class:`GreenClient` calls.
Green-specific identifiers (``idInstance``, ``apiTokenInstance``) live only
here and in :mod:`echo_v2.integrations.green.client`, mirroring the
provisioner's structure.

Credentials are resolved via the same :class:`CredentialResolver` pattern
used by :class:`GreenProvisioner` — the messaging service never owns storage.
"""

from __future__ import annotations

from echo_v2.integrations.green.client import GreenClient
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    CredentialResolver,
    ProviderCredentials,
    WhatsAppMessaging,
)
from echo_v2.runtime.errors import PermanentError

__all__ = ["GreenMessaging"]


class GreenMessaging:
    """Green API implementation of :class:`WhatsAppMessaging`."""

    def __init__(
        self,
        client: GreenClient,
        credential_resolver: CredentialResolver,
    ) -> None:
        self._client = client
        self._resolver = credential_resolver

    async def send_message(
        self,
        connection: ConnectionRef,
        chat_id: str,
        message: str,
    ) -> str:
        """Send a text message via Green ``sendMessage``.

        Returns the provider-assigned message id (``idMessage``).
        """
        api_token = await self._require_credentials(connection)
        return await self._client.send_message(
            connection.provider_connection_id,
            api_token,
            chat_id,
            message,
        )

    async def _require_credentials(self, ref: ConnectionRef) -> str:
        """Decode the API token for ``ref`` or raise.

        A missing credential is a data-integrity problem (the connection
        record was lost or never persisted), not a transient failure — raise
        :class:`PermanentError` so the runtime does not retry.
        """
        credentials = await self._resolver.get_credentials(ref)
        if credentials is None:
            raise PermanentError(
                f"no credentials stored for connection {ref.provider}:"
                f"{ref.provider_connection_id}"
            )
        return credentials.data.decode("utf-8")
