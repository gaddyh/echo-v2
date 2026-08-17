"""WhatsApp connection repository.

Owns the user -> provider connection mapping. The provider (Green adapter)
does **not** own storage; it talks to WhatsApp, this repository talks to the
database.

The :class:`WhatsAppConnectionRepository` protocol is intentionally minimal so
a future Postgres/Redis implementation can drop in without changing callers.
:class:`InMemoryWhatsAppConnectionRepository` is suitable for tests and Step 0
only -- it has no persistence across process restarts and no cross-process
coordination.

The webhook verification token is stored as a **hash** (plan guardrail G3),
not plaintext: the application generates the token, sends the plaintext to the
provider during provisioning, and stores ``sha256(token)`` here for later
``hmac.compare_digest`` validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ConnectionStatus,
    CredentialResolver,
    ProviderCredentials,
)

__all__ = [
    "InMemoryWhatsAppConnectionRepository",
    "StoredConnection",
    "WhatsAppConnectionRepository",
]


@dataclass(frozen=True)
class StoredConnection:
    """Persisted connection record.

    ``credentials`` is the opaque :class:`ProviderCredentials` (api token
    only). ``webhook_token_hash`` is ``sha256(plaintext_token)`` -- the
    plaintext is never stored.
    """

    user_id: str
    ref: ConnectionRef
    credentials: ProviderCredentials
    webhook_token_hash: bytes
    status: ConnectionStatus
    provider_raw_status: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class WhatsAppConnectionRepository:
    """Protocol-style base class for connection repositories.

    Subclasses implement the async methods. Kept as a regular class (not
    ``Protocol``) so it can carry docstrings and be subclassed directly by the
    in-memory impl; structural compatibility with
    :class:`CredentialResolver` is what matters to the provisioner.
    """

    async def save(self, conn: StoredConnection) -> None: ...
    async def get(self, ref: ConnectionRef) -> StoredConnection | None: ...
    async def get_by_user(self, user_id: str) -> StoredConnection | None: ...
    async def get_by_provider_id(
        self,
        provider: str,
        provider_id: str,
    ) -> StoredConnection | None: ...
    async def update_status(
        self,
        ref: ConnectionRef,
        status: ConnectionStatus,
        raw: str | None,
    ) -> None: ...
    async def get_credentials(
        self, ref: ConnectionRef
    ) -> ProviderCredentials | None: ...


class InMemoryWhatsAppConnectionRepository(WhatsAppConnectionRepository):
    """Process-local repository backed by dicts.

    Suitable for tests and Step 0 only. Implements :class:`CredentialResolver`
    via ``get_credentials`` so it can be injected straight into
    :class:`GreenProvisioner`.
    """

    def __init__(self) -> None:
        self._by_ref: dict[tuple[str, str], StoredConnection] = {}
        self._by_user: dict[str, tuple[str, str]] = {}
        self._by_provider_id: dict[tuple[str, str], tuple[str, str]] = {}

    async def save(self, conn: StoredConnection) -> None:
        key = (conn.ref.provider, conn.ref.provider_connection_id)
        self._by_ref[key] = conn
        self._by_user[conn.user_id] = key
        self._by_provider_id[key] = key

    async def get(self, ref: ConnectionRef) -> StoredConnection | None:
        return self._by_ref.get((ref.provider, ref.provider_connection_id))

    async def get_by_user(self, user_id: str) -> StoredConnection | None:
        key = self._by_user.get(user_id)
        return self._by_ref.get(key) if key else None

    async def get_by_provider_id(
        self,
        provider: str,
        provider_id: str,
    ) -> StoredConnection | None:
        key = self._by_provider_id.get((provider, provider_id))
        return self._by_ref.get(key) if key else None

    async def update_status(
        self,
        ref: ConnectionRef,
        status: ConnectionStatus,
        raw: str | None,
    ) -> None:
        key = (ref.provider, ref.provider_connection_id)
        existing = self._by_ref.get(key)
        if existing is None:
            return
        self._by_ref[key] = replace(
            existing,
            status=status,
            provider_raw_status=raw,
            updated_at=datetime.now(timezone.utc),
        )

    async def get_credentials(self, ref: ConnectionRef) -> ProviderCredentials | None:
        conn = self._by_ref.get((ref.provider, ref.provider_connection_id))
        return conn.credentials if conn else None


# Structural check: InMemoryWhatsAppConnectionRepository is a CredentialResolver.
_: CredentialResolver = InMemoryWhatsAppConnectionRepository()  # type: ignore[assignment]
