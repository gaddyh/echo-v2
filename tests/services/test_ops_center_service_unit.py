"""Unit tests for OpsCenterService user-health classification, caching,
and helpers that need no database (pure logic paths)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from echo_v2.services.ops_center_service import (
    PERIOD_SECONDS,
    OpsCenterService,
    OpsCenterSettings,
    _mask_phone,
)


def _service() -> OpsCenterService:
    return OpsCenterService(
        session_factory=None,  # type: ignore[arg-type]
        settings=OpsCenterSettings(),
    )


def test_user_health_worker_unavailable() -> None:
    service = _service()
    health, reason = service._user_health(
        provider="waha",
        connection_status="connected",
        status_age=10.0,
        worker_available=False,
        pending=0,
        exhausted=0,
        expired_leases=0,
    )
    assert health == "worker_unavailable"
    assert "stale" in reason


def test_user_health_disconnected_statuses() -> None:
    service = _service()
    for status in ("blocked", "suspended", "degraded"):
        health, reason = service._user_health(
            provider="baileys",
            connection_status=status,
            status_age=10.0,
            worker_available=True,
            pending=0,
            exhausted=0,
            expired_leases=0,
        )
        assert health == "disconnected"
        assert status in reason


def test_user_health_pairing_and_provisioning() -> None:
    service = _service()
    assert service._user_health(
        provider="baileys", connection_status="pairing_required", status_age=1.0,
        worker_available=True, pending=0, exhausted=0, expired_leases=0,
    )[0] == "awaiting_pairing"
    assert service._user_health(
        provider="baileys", connection_status="connecting", status_age=1.0,
        worker_available=True, pending=0, exhausted=0, expired_leases=0,
    )[0] == "provisioning"


def test_user_health_backlog_and_failures_before_staleness() -> None:
    service = _service()
    assert service._user_health(
        provider="baileys", connection_status="connected", status_age=10.0,
        worker_available=True, pending=0, exhausted=2, expired_leases=0,
    )[0] == "processing_failures"
    assert service._user_health(
        provider="baileys", connection_status="connected", status_age=10.0,
        worker_available=True, pending=3, exhausted=0, expired_leases=1,
    )[0] == "ingestion_backlog"


def test_user_health_stale_then_unknown() -> None:
    service = _service()
    assert service._user_health(
        provider="baileys", connection_status="connected", status_age=90000.0,
        worker_available=True, pending=0, exhausted=0, expired_leases=0,
    )[0] == "status_stale"
    assert service._user_health(
        provider="baileys", connection_status="unknown", status_age=None,
        worker_available=True, pending=0, exhausted=0, expired_leases=0,
    ) == ("unknown", "Connection status: unknown")


def test_mask_phone() -> None:
    assert _mask_phone("+972500000001") == "+97250000****"
    assert _mask_phone("1234") == "****"
    assert _mask_phone(None) == "unknown"


def test_period_table_and_default() -> None:
    assert set(PERIOD_SECONDS) == {"15m", "1h", "24h", "7d", "30d"}
    assert PERIOD_SECONDS["15m"] == 900


def test_snapshot_cache_returns_same_object_within_ttl() -> None:
    """The snapshot cache is exercised end-to-end in the Postgres tests;
    here we verify the clock-driven cache semantics with a stubbed build."""
    import asyncio

    service = _service()
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)
    service._clock = lambda: now  # type: ignore[method-assign]
    built: list[dict] = []

    async def fake_build(**kwargs):
        built.append(kwargs)
        return {"sections": {}, "generated_at": now.isoformat()}

    service._build_snapshot = fake_build  # type: ignore[method-assign]

    async def run() -> None:
        first = await service.snapshot(period="24h")
        second = await service.snapshot(period="24h")
        assert first is second
        assert len(built) == 1
        # A different filter combination misses the cache.
        await service.snapshot(period="24h", provider="waha")
        assert len(built) == 2
        # Advancing the clock past the TTL rebuilds.
        advanced = now + timedelta(seconds=30)
        service._clock = lambda: advanced  # type: ignore[method-assign]
        third = await service.snapshot(period="24h")
        assert third is not first
        assert len(built) == 3

    asyncio.run(run())
