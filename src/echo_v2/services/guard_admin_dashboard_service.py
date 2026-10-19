"""Read-only Guard/Baileys admin health snapshot."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from echo_v2.persistence.baileys_events import BaileysEventRepository
from echo_v2.persistence.guard_exposure_repositories import (
    GuardExposureAssessmentRepository,
)
from echo_v2.persistence.guard_simple_repositories import (
    GuardSimpleAssessmentRepository,
)
from echo_v2.persistence.whatsapp_connections import WhatsAppConnectionRepository
from echo_v2.ports.whatsapp import WhatsAppProvisioner


class UserInfoResolver(Protocol):
    async def get_user_info_by_id(self, user_id: str) -> tuple[str, str | None] | None: ...


class GuardAdminDashboardService:
    def __init__(
        self,
        *,
        connections: WhatsAppConnectionRepository,
        events: BaileysEventRepository,
        provisioner: WhatsAppProvisioner | None,
        users: UserInfoResolver,
        security: GuardSimpleAssessmentRepository,
        exposure: GuardExposureAssessmentRepository,
    ) -> None:
        self._connections = connections
        self._events = events
        self._provisioner = provisioner
        self._users = users
        self._security = security
        self._exposure = exposure

    async def snapshot(self) -> dict[str, Any]:
        rows = await self._connections.list_for_provider("baileys")
        live = await asyncio.gather(
            *(self._live_status(row) for row in rows), return_exceptions=True
        )
        names = await asyncio.gather(
            *(self._users.get_user_info_by_id(row.user_id) for row in rows)
        )
        connections: list[dict[str, Any]] = []
        for row, status, user_info in zip(rows, live, names):
            live_data = status if isinstance(status, dict) else {
                "status": "unavailable",
                "error": type(status).__name__,
            }
            connections.append({
                "connection_id": row.ref.provider_connection_id,
                "user_id": row.user_id,
                "child_name": user_info[1] if user_info and user_info[1] else row.user_id,

                "persisted_status": row.status.value,
                "persisted_raw_status": row.provider_raw_status,
                "updated_at": row.updated_at,
                "live": live_data,
            })
        child_ids = [row.user_id for row in rows]
        security, exposure = await asyncio.gather(
            self._security.list_for_children(child_user_ids=child_ids, limit=20),
            self._exposure.list_for_children(child_user_ids=child_ids, limit=20),
        )
        events = await self._events.recent_connection_events(limit=100)
        errors = await self._events.recent_processing_failures(limit=50)
        alerts = [
            {"kind": "security", **_assessment_view(item)}
            for item in security
            if item.parent_should_know is True
        ] + [
            {"kind": "exposure", **_assessment_view(item)}
            for item in exposure
            if item.significant_exposure is True
        ]
        alerts.sort(key=lambda item: item.get("created_at") or "", reverse=True)
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
            "alerts": alerts[:30],
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


def _assessment_view(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "child_name": item.child_name,
        "chat_name": item.chat_name,
        "decision": getattr(item, "decision", None) or getattr(item, "significant_exposure", None),
        "category": getattr(item, "category", None),
        "summary": item.summary,
        "reason": item.reason,
        "created_at": item.created_at,
    }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
