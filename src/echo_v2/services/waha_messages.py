"""Parse GOWS message envelopes from the WAHA event inbox.

The batch agent stores the raw GOWS (Whatsmeow-based) serialization:
top-level Go-style keys (``Info``, ``Message``, ``IsReal``…) with the
inner ``Message`` in protobuf-JSON style (``conversation``,
``audioMessage``…). This module is the adapter boundary: GOWS JSON never
escapes it — everything downstream sees the provider-neutral
:class:`ProviderMessageEvent` contract, exactly like the Green and Baileys
adapters.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from echo_v2.domain.waha import WahaInboxMessage, WahaMediaPayload
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    MessageDirection,
    MessageKind,
    MessageSource,
    ProviderMessageEvent,
)

__all__ = ["extract_media", "parse_envelope"]

_logger = logging.getLogger("echo_v2.services.waha_messages")

_PROVIDER = "waha"

# Message key → (provider-neutral kind, WAHA media type for HKDF info).
_MEDIA_KEYS: dict[str, tuple[MessageKind, str]] = {
    "audioMessage": (MessageKind.AUDIO, "audio"),
    "imageMessage": (MessageKind.IMAGE, "image"),
    "videoMessage": (MessageKind.VIDEO, "video"),
    "documentMessage": (MessageKind.DOCUMENT, "document"),
    "stickerMessage": (MessageKind.OTHER, "sticker"),
}


def _required_dict(payload: dict[str, Any], field: str) -> dict[str, Any]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise TypeError(f"GOWS envelope is missing {field}")
    return value


def _message_content(payload: dict[str, Any]) -> dict[str, Any] | None:
    message = payload.get("Message")
    if message is None:
        return None
    if not isinstance(message, dict):
        raise TypeError("GOWS envelope has invalid Message")
    return message


def _text_of(message: dict[str, Any] | None) -> str | None:
    """Extract the body text: plain/extended text, else a media caption."""
    if message is None:
        return None
    conversation = message.get("conversation")
    if isinstance(conversation, str) and conversation:
        return conversation
    extended = message.get("extendedTextMessage")
    if isinstance(extended, dict) and isinstance(extended.get("text"), str):
        return str(extended["text"])
    for key in ("imageMessage", "videoMessage", "documentMessage"):
        media = message.get(key)
        if isinstance(media, dict) and isinstance(media.get("caption"), str):
            caption: str = media["caption"]
            return caption or None
    return None


def _kind_of(message: dict[str, Any] | None) -> MessageKind:
    if message is None:
        return MessageKind.OTHER
    if "conversation" in message or "extendedTextMessage" in message:
        return MessageKind.TEXT
    for key, (kind, _media_type) in _MEDIA_KEYS.items():
        if key in message:
            return kind
    if "reactionMessage" in message:
        return MessageKind.REACTION
    return MessageKind.OTHER


def _bool_field(info: dict[str, Any], field: str) -> bool | None:
    value = info.get(field)
    if isinstance(value, bool):
        return value
    return None


def extract_media(message: WahaInboxMessage) -> WahaMediaPayload | None:
    """Return the media descriptor for a message, if it carries one.

    Only messages with both a CDN ``URL`` and a ``mediaKey`` are considered
    downloadable — anything else is fundamentally unavailable media (the
    enrichment layer marks it terminal instead of retrying).
    """
    try:
        envelope = _parse_json(message.data)
    except ValueError:
        return None
    content = _message_content(envelope)
    if content is None:
        return None
    for key, (kind, media_type) in _MEDIA_KEYS.items():
        media = content.get(key)
        if not isinstance(media, dict):
            continue
        url = media.get("URL")
        media_key = media.get("mediaKey")
        if not isinstance(url, str) or not url:
            return None
        if not isinstance(media_key, str) or not media_key:
            return None
        file_length_raw = media.get("fileLength")
        file_sha256 = media.get("fileSHA256")
        # GOWS serializes int64 fields as JSON strings ("7023").
        if isinstance(file_length_raw, str) and file_length_raw.isdigit():
            file_length_raw = int(file_length_raw)
        if not isinstance(file_length_raw, int) or not isinstance(file_sha256, str):
            return None
        return WahaMediaPayload(
            media_type=media_type,
            kind=kind.value,
            url=url,
            media_key=media_key,
            file_length=file_length_raw,
            file_sha256=file_sha256,
            mimetype=media.get("mimetype") if isinstance(media.get("mimetype"), str) else None,
            file_name=media.get("fileName") if isinstance(media.get("fileName"), str) else None,
        )
    return None


def _parse_json(data: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("GOWS envelope is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise TypeError("GOWS envelope must be a JSON object")
    return payload


def parse_envelope(message: WahaInboxMessage) -> ProviderMessageEvent:
    """Normalize one inbox row into the provider-neutral message contract."""
    envelope = _parse_json(message.data)
    info = _required_dict(envelope, "Info")
    content = _message_content(envelope)

    chat_id = info.get("Chat")
    if not isinstance(chat_id, str) or not chat_id:
        raise ValueError("GOWS envelope is missing Info.Chat")

    is_from_me = _bool_field(info, "IsFromMe")
    if is_from_me is None:
        raise ValueError("GOWS envelope is missing Info.IsFromMe")

    sender_id = info.get("Sender")
    if sender_id is not None and not isinstance(sender_id, str):
        raise ValueError("GOWS envelope has invalid Info.Sender")
    push_name = info.get("PushName")
    if push_name is not None and not isinstance(push_name, str):
        raise ValueError("GOWS envelope has invalid Info.PushName")

    media = extract_media(message)
    kind = _kind_of(content)
    media_reference = (
        f"{_PROVIDER}:{message.session}:{message.message_id}"
        if media is not None
        else None
    )

    direction = MessageDirection.OUTBOUND if is_from_me else MessageDirection.INBOUND
    _logger.info(
        "waha envelope parsed session=%s message_id=%s chat_id=%s kind=%s "
        "direction=%s is_group=%s has_media=%s",
        message.session,
        message.message_id,
        chat_id,
        kind.value,
        direction.value,
        _bool_field(info, "IsGroup"),
        media is not None,
    )
    return ProviderMessageEvent(
        event_id=f"{_PROVIDER}:{message.session}:{message.message_id}",
        connection=ConnectionRef(
            provider=_PROVIDER, provider_connection_id=message.session
        ),
        chat_id=chat_id,
        provider_message_id=message.message_id,
        direction=direction,
        source=MessageSource.USER if is_from_me else None,
        timestamp=message.whatsapp_timestamp,
        kind=kind,
        text=_text_of(content),
        sender_id=sender_id,
        sender_name=push_name,
        chat_name=None,
        is_group=_bool_field(info, "IsGroup"),
        media_reference=media_reference,
        media_download_url=None,
        media_mime_type=media.mimetype if media else None,
        media_file_name=media.file_name if media else None,
        media_size=media.file_length if media else None,
    )
