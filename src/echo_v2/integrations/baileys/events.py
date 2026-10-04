"""Normalized events emitted by the Baileys connector service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ConnectionStatus,
    MessageDirection,
    MessageKind,
    MessageSource,
    ProviderMessageEvent,
)


@dataclass(frozen=True)
class BaileysConnectionStateEvent:
    event_id: str
    inbox_id: int
    connection_id: str
    status: ConnectionStatus
    provider_raw_status: str | None
    timestamp: datetime
    state_version: int | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any], inbox_id: int) -> BaileysConnectionStateEvent:
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported Baileys event schema version")
        if payload.get("event_type") != "connection_state":
            raise ValueError("unsupported Baileys connector event type")
        if payload.get("provider") != "baileys":
            raise ValueError("unexpected event provider")
        event_id = payload.get("event_id")
        connection_id = payload.get("connection_id")
        status = payload.get("status")
        timestamp = payload.get("timestamp")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event is missing event_id")
        if not isinstance(connection_id, str) or not connection_id:
            raise ValueError("event is missing connection_id")
        if not isinstance(status, str):
            raise TypeError("event is missing status")
        try:
            normalized_status = ConnectionStatus(status)
        except ValueError as exc:
            raise ValueError(f"unknown Baileys connection status: {status}") from exc
        event_timestamp = _timestamp(timestamp)
        raw = payload.get("provider_raw_status")
        if raw is not None and not isinstance(raw, str):
            raise ValueError("event has invalid provider_raw_status")
        version = payload.get("state_version")
        if version is not None and not isinstance(version, int):
            raise ValueError("event has invalid state_version")
        return cls(
            event_id=event_id,
            inbox_id=inbox_id,
            connection_id=connection_id,
            status=normalized_status,
            provider_raw_status=raw,
            timestamp=event_timestamp,
            state_version=version,
        )


class BaileysMessageEvent:
    """Parser for the connector's versioned provider-neutral message contract."""

    @staticmethod
    def from_payload(payload: dict[str, Any]) -> ProviderMessageEvent:
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported Baileys event schema version")
        if payload.get("event_type") != "message":
            raise ValueError("unsupported Baileys connector event type")
        if payload.get("provider") != "baileys":
            raise ValueError("unexpected event provider")

        event_id = _required_string(payload, "event_id")
        connection_id = _required_string(payload, "connection_id")
        chat_id = _required_string(payload, "chat_id")
        provider_message_id = _required_string(payload, "provider_message_id")
        timestamp = _timestamp(payload.get("timestamp"))

        direction_raw = payload.get("direction")
        try:
            direction = MessageDirection(direction_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("event has invalid direction") from exc

        source_raw = payload.get("source")
        if source_raw is not None and source_raw not in {item.value for item in MessageSource if item}:
            raise ValueError("event has invalid source")
        source = MessageSource(source_raw) if source_raw is not None else None

        kind_raw = payload.get("kind", "other")
        try:
            kind = MessageKind(kind_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("event has invalid message kind") from exc

        is_group = payload.get("is_group")
        if not isinstance(is_group, bool):
            raise TypeError("event is missing valid is_group")

        sender = payload.get("sender")
        sender_id: str | None = None
        sender_name: str | None = None
        if sender is not None:
            if not isinstance(sender, dict):
                raise ValueError("event has invalid sender")
            sender_id = _optional_string(sender, "canonical_id")
            sender_name = _optional_string(sender, "display_name")

        return ProviderMessageEvent(
            event_id=event_id,
            connection=ConnectionRef("baileys", connection_id),
            chat_id=chat_id,
            provider_message_id=provider_message_id,
            direction=direction,
            source=source,
            timestamp=timestamp,
            kind=kind,
            text=_optional_string(payload, "text"),
            sender_id=sender_id,
            sender_name=sender_name,
            chat_name=_optional_string(payload, "chat_name"),
            is_group=is_group,
            media_reference=_optional_string(payload, "media_reference"),
            media_download_url=_optional_string(payload, "media_download_url"),
            media_mime_type=_optional_string(payload, "media_mime_type"),
            media_file_name=_optional_string(payload, "media_file_name"),
        )


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"event is missing {field}")
    return value


def _optional_string(payload: dict[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"event has invalid {field}")
    return value


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("event is missing timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("event has invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("event timestamp must include timezone")
    return parsed
