"""Normalized events emitted by the Baileys connector service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from echo_v2.ports.whatsapp import ConnectionStatus


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
        if not isinstance(timestamp, str):
            raise TypeError("event is missing timestamp")
        try:
            event_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("event has invalid timestamp") from exc
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
