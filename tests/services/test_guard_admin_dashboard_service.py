"""Tests for the read-only Guard admin dashboard snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from echo_v2.persistence.whatsapp_connections import StoredConnection
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ConnectionStatus,
    ConnectionStatusSnapshot,
    ProviderCredentials,
)
from echo_v2.services.guard_admin_dashboard_service import GuardAdminDashboardService


class _Connections:
    def __init__(self, rows: list[StoredConnection]) -> None:
        self.rows = rows

    async def list_for_provider(self, provider: str) -> list[StoredConnection]:
        return [row for row in self.rows if row.ref.provider == provider]


class _Events:
    async def recent_connection_events(self, *, limit: int):
        return [{"event": "connected"}]

    async def recent_processing_failures(self, *, limit: int):
        return [{"event": "failed"}]


class _Users:
    async def get_user_info_by_id(self, user_id: str):
        return (f"+9725{user_id}", "Child") if user_id == "connected" else None


class _Assessments:
    def __init__(self, security, exposure) -> None:
        self.security = security
        self.exposure = exposure

    async def list_for_children(self, *, child_user_ids: list[str], limit: int):
        return self.security if child_user_ids and self.security else self.exposure


class _Provisioner:
    async def get_status(self, ref: ConnectionRef) -> ConnectionStatusSnapshot:
        if ref.provider_connection_id == "broken":
            raise RuntimeError("provider unavailable")
        status = ConnectionStatus.CONNECTED if ref.provider_connection_id == "connected" else ConnectionStatus.PAIRING_REQUIRED
        return ConnectionStatusSnapshot(status=status, provider_raw_status=status.value)


def _row(user_id: str, connection_id: str) -> StoredConnection:
    return StoredConnection(
        user_id=user_id,
        ref=ConnectionRef("baileys", connection_id),
        credentials=ProviderCredentials(data=b""),
        webhook_token_hash=b"hash",
        status=ConnectionStatus.CONNECTED,
    )


@pytest.mark.asyncio
async def test_snapshot_collects_live_status_counts_alerts_and_failures() -> None:
    now = datetime.now(timezone.utc)
    security = [SimpleNamespace(
        id="security-1", child_name="Child", chat_name="Class", decision=True,
        category="pressure", summary="Repeated pressure", reason="pattern", created_at=now,
        parent_should_know=True,
    )]
    exposure = [SimpleNamespace(
        id="exposure-1", child_name="Other", chat_name="Group", significant_exposure=True,
        category="bullying", summary="Group incident", reason="severity", created_at=now,
    )]
    service = GuardAdminDashboardService(
        connections=_Connections([_row("connected", "connected"), _row("broken", "broken")]),
        events=_Events(),
        provisioner=_Provisioner(),
        users=_Users(),
        security=_Assessments(security, []),
        exposure=_Assessments([], exposure),
    )

    snapshot = await service.snapshot()

    assert snapshot["counts"]["total"] == 2
    assert snapshot["counts"]["connected"] == 1
    assert snapshot["counts"]["degraded"] == 1
    assert snapshot["connections"][0]["child_name"] == "Child"
    assert snapshot["connections"][1]["live"]["status"] == "unavailable"
    assert {alert["kind"] for alert in snapshot["alerts"]} == {"security", "exposure"}
    assert snapshot["events"] and snapshot["errors"]


@pytest.mark.asyncio
async def test_snapshot_without_provisioner_reports_unavailable() -> None:
    service = GuardAdminDashboardService(
        connections=_Connections([_row("child", "connection")]),
        events=_Events(),
        provisioner=None,
        users=_Users(),
        security=_Assessments([], []),
        exposure=_Assessments([], []),
    )

    snapshot = await service.snapshot()

    assert snapshot["connections"][0]["live"] == {
        "status": "unavailable",
        "reason": "Baileys provisioner is not configured",
    }
