"""Read-only Guard/Baileys admin health snapshot."""

from __future__ import annotations

import asyncio
from typing import Any

from echo_v2.persistence.baileys_events import BaileysEventRepository
from echo_v2.persistence.whatsapp_connections import WhatsAppConnectionRepository
from echo_v2.ports.whatsapp import WhatsAppProvisioner


class GuardAdminDashboardService:
    def __init__(
        self,
        *,
        connections: WhatsAppConnectionRepository,
        events: BaileysEventRepository,
        provisioner: WhatsAppProvisioner | None,
    ) -> None:
        self._connections = connections
        self._events = events
        self._provisioner = provisioner

    async def snapshot(self) -> dict[str, Any]:
        rows = await self._connections.list_for_provider("baileys")
        live = await asyncio.gather(
            *(self._live_status(row) for row in rows), return_exceptions=True
        )
        connections: list[dict[str, Any]] = []
        for row, status in zip(rows, live):
            live_data = status if isinstance(status, dict) else {
                "status": "unavailable",
                "error": type(status).__name__,
            }
            connections.append({
                "connection_id": row.ref.provider_connection_id,
                "user_id": row.user_id,
                "persisted_status": row.status.value,
                "persisted_raw_status": row.provider_raw_status,
                "updated_at": row.updated_at,
                "live": live_data,
            })
        events = await self._events.recent_connection_events(limit=100)
        errors = await self._events.recent_processing_failures(limit=50)
        counts = {
            "total": len(connections),
            "connected": sum(
                item["live"].get("status") == "connected" for item in connections
            ),
            "pairing_required": sum(
                item["live"].get("status") == "pairing_required" for item in connections
            ),
            "degraded": sum(
                item["live"].get("status") in {"degraded", "unknown", "unavailable"}
                for item in connections
            ),
        }
        return {
            "generated_at": _now_iso(),
            "counts": counts,
            "connections": connections,
            "events": events,
            "errors": errors,
        }

    async def _live_status(self, row: Any) -> dict[str, Any]:
        if self._provisioner is None:
            return {"status": "unavailable", "reason": "Baileys provisioner is not configured"}
        snapshot = await self._provisioner.get_status(row.ref)
        return {
            "status": snapshot.status.value,
            "provider_raw_status": snapshot.provider_raw_status,
            "phone_number_present": snapshot.phone_number is not None,
            "checked_at": snapshot.checked_at,
        }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
