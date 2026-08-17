"""Green webhook JSON -> canonical provider events.

This is the most important Green boundary in the system (plan decision 9).
The adapter is pure and synchronous: no I/O, no user lookup, no raw payload
on output. It emits **provider events** carrying a :class:`ConnectionRef`
(no ``user_id``); the application layer resolves the connection to a user and
emits the domain event.

Green ``typeWebhook`` values map to:

* ``incomingMessageReceived``        -> ``ProviderMessageEvent(INBOUND, source=None)``
* ``outgoingMessageReceived``        -> ``ProviderMessageEvent(OUTBOUND, source=USER)``
* ``outgoingAPIMessageReceived``     -> ``ProviderMessageEvent(OUTBOUND, source=API)``
* ``outgoingMessageStatus``          -> ``ProviderMessageStatusEvent``
* ``stateInstanceChanged``           -> ``ProviderConnectionStateChanged``
* anything else                      -> ``None`` (logged at INFO by the caller)

Green-native ``typeMessage`` values (``textMessage``, ``extendedTextMessage``,
``imageMessage``, ...) map to the neutral :class:`MessageKind` enum; unknown
types map to ``OTHER``. Green JSON never escapes this adapter -- no ``raw``
field on any event.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from echo_v2.integrations.green.models import map_state
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    MessageDirection,
    MessageKind,
    MessageSource,
    ProviderConnectionStateChanged,
    ProviderEvent,
    ProviderMessageEvent,
    ProviderMessageStatusEvent,
    WhatsAppEventAdapter,
)

__all__ = ["GreenEventAdapter"]

_logger = logging.getLogger("echo_v2.green.events")


# Green ``typeWebhook`` values.
_TYPE_INCOMING = "incomingMessageReceived"
_TYPE_OUTGOING = "outgoingMessageReceived"
_TYPE_OUTGOING_API = "outgoingAPIMessageReceived"
_TYPE_OUTGOING_STATUS = "outgoingMessageStatus"
_TYPE_STATE_CHANGED = "stateInstanceChanged"

# Green ``typeMessage`` -> neutral MessageKind.
_KIND_MAP: dict[str, MessageKind] = {
    "textMessage": MessageKind.TEXT,
    "extendedTextMessage": MessageKind.TEXT,
    "quotedMessage": MessageKind.TEXT,
    "imageMessage": MessageKind.IMAGE,
    "audioMessage": MessageKind.AUDIO,
    "videoMessage": MessageKind.VIDEO,
    "documentMessage": MessageKind.DOCUMENT,
    "reactionMessage": MessageKind.REACTION,
}


class GreenEventAdapter:
    """Implements :class:`WhatsAppEventAdapter` for Green API webhooks."""

    def parse(self, payload: dict[str, Any]) -> ProviderEvent | None:
        if not isinstance(payload, dict):
            return None
        type_webhook = payload.get("typeWebhook")
        if type_webhook is None:
            return None

        ref = _connection_ref(payload)
        if ref is None:
            return None

        if type_webhook == _TYPE_INCOMING:
            return _message_event(payload, ref, MessageDirection.INBOUND, source=None)
        if type_webhook == _TYPE_OUTGOING:
            return _message_event(
                payload, ref, MessageDirection.OUTBOUND, source=MessageSource.USER
            )
        if type_webhook == _TYPE_OUTGOING_API:
            return _message_event(
                payload, ref, MessageDirection.OUTBOUND, source=MessageSource.API
            )
        if type_webhook == _TYPE_OUTGOING_STATUS:
            return _status_event(payload, ref)
        if type_webhook == _TYPE_STATE_CHANGED:
            return _state_event(payload, ref)

        _logger.info("provider=green event=unknown type_webhook=%s", type_webhook)
        return None


def _connection_ref(payload: dict[str, Any]) -> ConnectionRef | None:
    instance_data = payload.get("instanceData")
    if not isinstance(instance_data, dict):
        return None
    instance_id = instance_data.get("idInstance")
    if instance_id is None:
        return None
    return ConnectionRef(provider="green", provider_connection_id=str(instance_id))


def _message_event(
    payload: dict[str, Any],
    ref: ConnectionRef,
    direction: MessageDirection,
    *,
    source: MessageSource | None,
) -> ProviderMessageEvent | None:
    message_data = payload.get("messageData")
    if not isinstance(message_data, dict):
        return None
    type_message = message_data.get("typeMessage", "textMessage")
    kind = _KIND_MAP.get(type_message, MessageKind.OTHER)
    if kind is MessageKind.OTHER:
        _logger.info("provider=green event=message unknown_type=%s", type_message)

    chat_id = payload.get("chatId") or _extract_chat_id(message_data)
    provider_message_id = payload.get("idMessage")
    timestamp = _timestamp(payload.get("timestamp"))
    text = _extract_text(message_data, type_message)

    if not chat_id or not provider_message_id:
        return None

    return ProviderMessageEvent(
        connection=ref,
        chat_id=str(chat_id),
        provider_message_id=str(provider_message_id),
        direction=direction,
        source=source,
        timestamp=timestamp,
        kind=kind,
        text=text,
    )


def _status_event(
    payload: dict[str, Any],
    ref: ConnectionRef,
) -> ProviderMessageStatusEvent | None:
    message_data = payload.get("messageData")
    if not isinstance(message_data, dict):
        return None
    provider_message_id = payload.get("idMessage")
    status = message_data.get("statusWebhook") or payload.get("status")
    timestamp = _timestamp(payload.get("timestamp"))
    if not provider_message_id or not status:
        return None
    return ProviderMessageStatusEvent(
        connection=ref,
        provider_message_id=str(provider_message_id),
        status=str(status),
        timestamp=timestamp,
    )


def _state_event(
    payload: dict[str, Any],
    ref: ConnectionRef,
) -> ProviderConnectionStateChanged:
    # instanceData is already validated as a dict by _connection_ref.
    instance_data = payload["instanceData"]
    raw = instance_data.get("stateInstance")
    timestamp = _timestamp(payload.get("timestamp"))
    return ProviderConnectionStateChanged(
        connection=ref,
        status=map_state(raw if isinstance(raw, str) else None),
        provider_raw_status=raw if isinstance(raw, str) else None,
        timestamp=timestamp,
    )


def _extract_text(message_data: dict[str, Any], type_message: str) -> str | None:
    if type_message in ("textMessage", "quotedMessage"):
        text = message_data.get("textMessage")
        return str(text) if text else None
    if type_message == "extendedTextMessage":
        ext = message_data.get("extendedTextMessage")
        if isinstance(ext, dict):
            text = ext.get("text")
            if text:
                return str(text)
        # Fall back to a top-level textMessage if the extended envelope has no text.
        text = message_data.get("textMessage")
        return str(text) if text else None
    # Non-text messages may carry a caption.
    caption = message_data.get("caption")
    return str(caption) if caption else None


def _extract_chat_id(message_data: dict[str, Any]) -> str | None:
    chat = message_data.get("chatId")
    return str(chat) if chat else None


def _timestamp(raw: Any) -> datetime:
    """Convert a Green epoch timestamp to timezone-aware UTC.

    Green sends UNIX seconds or milliseconds. Always use
    ``datetime.fromtimestamp(..., timezone.utc)`` (plan guardrail G9).
    """
    if raw is None:
        return datetime.now(timezone.utc)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if value > 10_000_000_000:
        value /= 1000.0
    return datetime.fromtimestamp(value, timezone.utc)


# Structural check: GreenEventAdapter is a WhatsAppEventAdapter.
_: WhatsAppEventAdapter = GreenEventAdapter()  # type: ignore[assignment]
