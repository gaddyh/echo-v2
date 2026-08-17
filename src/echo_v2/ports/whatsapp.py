"""Provider-neutral WhatsApp ports and types.

This module is the boundary between Echo's application/domain code and any
WhatsApp transport (Green API today, Meta Cloud API later). It must remain
free of integration-layer dependencies so the pure runtime can be imported
and tested without ``httpx``/``websockets``/``fastapi``.

Three ports are defined here:

* :class:`WhatsAppProvisioner`  -- connection lifecycle (create/configure/
  status/QR/unpair/delete). Owns no messaging.
* :class:`WhatsAppMessaging`    -- send/read (scaffolded only; implemented in
  a later milestone).
* :class:`WhatsAppEventAdapter` -- provider webhook JSON -> canonical provider
  events.

Provider-specific identifiers (``idInstance``, ``apiTokenInstance``,
``stateInstance`` for Green) never cross this boundary. The product sees
:class:`ConnectionRef` and :class:`ConnectionStatus`.

The event model is two-layered:

* **Provider events** (defined here) carry a :class:`ConnectionRef` and never
  a ``user_id`` and never the raw provider payload.
* **Domain events** (defined later in ``echo_v2.domain``) carry a ``user_id``
  and are produced by the application layer after resolving a connection to a
  user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, runtime_checkable

__all__ = [
    "ConnectionConfig",
    "ConnectionRef",
    "ConnectionStatus",
    "ConnectionStatusSnapshot",
    "CreatedConnection",
    "CredentialResolver",
    "MessageDirection",
    "MessageKind",
    "MessageSource",
    "PairingOutcome",
    "PairingQr",
    "PairingResult",
    "ProviderConnectionStateChanged",
    "ProviderCredentials",
    "ProviderEvent",
    "ProviderMessageEvent",
    "ProviderMessageStatusEvent",
    "WhatsAppEventAdapter",
    "WhatsAppEventSubscription",
    "WhatsAppMessaging",
    "WhatsAppProvisioner",
]


# --- Status ---------------------------------------------------------------


class ConnectionStatus(Enum):
    """Provider-neutral operational state of a WhatsApp connection.

    Mapped from each provider's raw status at the integration boundary. The
    raw provider string is retained alongside (``provider_raw_status``) for
    debugging only; application code must branch on this enum, never on the
    raw string.
    """

    PROVISIONING = "provisioning"
    CONNECTING = "connecting"
    PAIRING_REQUIRED = "pairing_required"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


# --- Identity & credentials ----------------------------------------------


@dataclass(frozen=True)
class ConnectionRef:
    """Long-lived, provider-neutral identity for a WhatsApp connection.

    ``provider_connection_id`` is opaque to all callers except the integration
    that produced it. For Green it is the ``idInstance``; the per-instance API
    token is *not* carried here -- it lives in :class:`ProviderCredentials`.
    """

    provider: str
    provider_connection_id: str


@dataclass(frozen=True, repr=False)
class ProviderCredentials:
    """Opaque credential carrier returned at the provisioning boundary.

    Contains **only** the secret credential (e.g. Green's ``apiTokenInstance``).
    The connection identity (``idInstance``) lives in
    :class:`ConnectionRef.provider_connection_id`, so the two stored identities
    can never disagree.

    ``repr`` is disabled so credentials cannot be accidentally printed in
    logs, tracebacks, or test assertions. The integration that produced the
    bytes is the only code that knows how to decode them; persistence only
    stores them; the application never opens them.
    """

    data: bytes

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return "ProviderCredentials(<redacted>)"


@dataclass(frozen=True)
class CreatedConnection:
    """Result of :meth:`WhatsAppProvisioner.create_connection`.

    Exists only at the provisioning boundary. The application persists both
    fields immediately (see plan guardrail G2: retry persistence, not
    ``create_connection``), after which only ``ref`` is used as the long-lived
    identity.
    """

    ref: ConnectionRef
    credentials: ProviderCredentials


# --- Configuration --------------------------------------------------------


@dataclass(frozen=True)
class WhatsAppEventSubscription:
    """Which WhatsApp event classes Echo wants delivered via webhook.

    Provider-neutral; each integration translates this to its own webhook
    switches. Defaults match the Echo MVP need set: incoming messages,
    phone/user-originated outgoing messages, API-originated outgoing messages,
    outgoing delivery statuses, and connection-state changes. Everything else
    (polls, calls, edits, deletes, catalog, device/battery) is off.
    """

    incoming_messages: bool = True
    outgoing_phone_messages: bool = True
    outgoing_api_messages: bool = True
    message_statuses: bool = True
    connection_state: bool = True


@dataclass(frozen=True)
class ConnectionConfig:
    """Configuration applied during connection creation/reconfiguration.

    ``webhook_token`` is an Echo-generated plaintext secret (e.g.
    ``secrets.token_urlsafe(32)``) that the application sends to the provider
    as the webhook auth token and stores as a hash for later verification
    (plan guardrail G3). The provisioner never generates it.
    """

    webhook_url: str
    webhook_token: str
    subscriptions: WhatsAppEventSubscription = field(
        default_factory=WhatsAppEventSubscription
    )


# --- Status snapshot ------------------------------------------------------


@dataclass(frozen=True)
class ConnectionStatusSnapshot:
    """Point-in-time status of a connection, as pulled from the provider."""

    status: ConnectionStatus
    provider_raw_status: str | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# --- QR / pairing ---------------------------------------------------------


@dataclass(frozen=True)
class PairingQr:
    """A QR image the user scans to pair their phone."""

    image_base64: str
    expires_at: datetime | None = None


class PairingOutcome(Enum):
    """Outcome of a pairing-QR request.

    ``PASSKEY_REQUIRED`` is surfaced explicitly so the client never treats it
    as malformed input; PassKey onboarding itself is not implemented in this
    milestone.
    """

    QR_READY = "qr_ready"
    ALREADY_AUTHORIZED = "already_authorized"
    PASSKEY_REQUIRED = "passkey_required"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class PairingResult:
    outcome: PairingOutcome
    qr: PairingQr | None = None
    message: str | None = None


# --- Provider events ------------------------------------------------------


class MessageDirection(Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageSource(Enum):
    """Origin of an outbound message.

    ``USER`` covers any user-originated outgoing message -- phone, WhatsApp
    Desktop, or another linked device. ``API`` is Echo acting through the
    provider API.
    """

    USER = "user"
    API = "api"


class MessageKind(Enum):
    """Provider-neutral message type.

    Provider-native type labels (e.g. Green's ``extendedTextMessage``,
    ``quotedMessage``) are mapped to this enum at the adapter boundary and
    never leak through. Unknown provider types map to ``OTHER`` (and are
    logged at the integration boundary).
    """

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"
    REACTION = "reaction"
    OTHER = "other"


@dataclass(frozen=True)
class ProviderMessageEvent:
    """Normalized inbound or outbound message event from a provider webhook.

    Carries a :class:`ConnectionRef` (no ``user_id``); the application layer
    resolves the connection to a user and emits the domain event. Carries no
    raw provider payload -- "provider JSON never escapes the adapter".
    """

    connection: ConnectionRef
    chat_id: str
    provider_message_id: str
    direction: MessageDirection
    source: MessageSource | None
    timestamp: datetime
    kind: MessageKind = MessageKind.TEXT
    text: str | None = None


@dataclass(frozen=True)
class ProviderMessageStatusEvent:
    """Delivery status update for a previously-sent message."""

    connection: ConnectionRef
    provider_message_id: str
    status: str
    timestamp: datetime


@dataclass(frozen=True)
class ProviderConnectionStateChanged:
    """Connection authorization/state change pushed by the provider."""

    connection: ConnectionRef
    status: ConnectionStatus
    provider_raw_status: str | None
    timestamp: datetime


ProviderEvent = (
    ProviderMessageEvent | ProviderMessageStatusEvent | ProviderConnectionStateChanged
)


# --- Ports ----------------------------------------------------------------


@runtime_checkable
class WhatsAppProvisioner(Protocol):
    """Connection lifecycle port. Owns no messaging, stores nothing."""

    async def create_connection(
        self,
        config: ConnectionConfig,
    ) -> CreatedConnection:
        """Create and configure a new connection in one provider call.

        Returns a :class:`CreatedConnection` whose ``ref`` is the long-lived
        identity and whose ``credentials`` is the opaque secret to persist.
        Provisioning is an irreversible write (plan guardrail G1): a timeout
        means the instance may exist on the provider side, and the caller must
        not blindly retry ``create_connection``.
        """
        ...

    async def configure_connection(
        self,
        connection: ConnectionRef,
        config: ConnectionConfig,
    ) -> None:
        """Apply webhook/subscription settings to an existing connection."""
        ...

    async def get_status(
        self,
        connection: ConnectionRef,
    ) -> ConnectionStatusSnapshot:
        """Pull current connection status from the provider."""
        ...

    async def get_pairing_qr(
        self,
        connection: ConnectionRef,
    ) -> PairingResult:
        """Obtain a pairing QR (or an alternative outcome such as
        already-authorized / passkey-required)."""
        ...

    async def unpair(
        self,
        connection: ConnectionRef,
    ) -> None:
        """Unpair the phone but keep the instance for re-pairing."""
        ...

    async def delete_connection(
        self,
        connection: ConnectionRef,
    ) -> None:
        """Fully tear down the connection on the provider side."""
        ...


@runtime_checkable
class WhatsAppEventAdapter(Protocol):
    """Provider webhook JSON -> canonical provider events.

    Pure and synchronous: no I/O, no user lookup, no raw payload on output.
    """

    def parse(self, payload: dict) -> ProviderEvent | None:
        """Normalize a provider webhook payload.

        Returns ``None`` for payloads the adapter deliberately ignores
        (unknown ``typeWebhook``). Never raises on unknown shapes -- returns
        ``None`` or ``OTHER``-typed events so an unexpected provider field
        never breaks the webhook ingress.
        """
        ...


@runtime_checkable
class CredentialResolver(Protocol):
    """Resolve opaque credentials for a connection.

    Implemented by the connection repository; consumed by the provisioner so
    the provisioner never owns storage.
    """

    async def get_credentials(
        self,
        ref: ConnectionRef,
    ) -> ProviderCredentials | None: ...


@runtime_checkable
class WhatsAppMessaging(Protocol):
    """Send/read port. Scaffolded only; implemented in a later milestone."""

    async def send_message(
        self,
        connection: ConnectionRef,
        chat_id: str,
        message: str,
    ) -> str:
        """Send a text message; returns the provider message id."""
        ...
