"""DailyStatsWorker tests — lock, rebuild, and idempotency."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.persistence.postgres_guard_daily_stats import (
    PostgresGuardDailyStatsRepository,
)
from echo_v2.services.reporting.daily_stats_worker import DailyStatsWorker
from echo_v2.services.reporting.guard_reporting_service import (
    GuardReportingService,
    ReportingConfig,
)
from tests.persistence.conftest import insert_user

pytestmark = pytest.mark.asyncio


async def _insert_guardian_link(
    session_factory: async_sessionmaker,
    *,
    guardian_user_id: str,
    child_user_id: str,
) -> None:
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO guardian_child_links "
                "(guardian_user_id, child_user_id, status, created_at) "
                "VALUES (:gid, :cid, 'active', :now)"
            ),
            {
                "gid": guardian_user_id,
                "cid": child_user_id,
                "now": datetime.now(timezone.utc),
            },
        )
        await session.commit()


async def _insert_connection(
    session_factory: async_sessionmaker,
    user_id: str,
    status: str = "connected",
) -> str:
    from echo_v2.persistence.credential_cipher import IdentityCredentialCipher

    cipher = IdentityCredentialCipher()
    creds = cipher.encrypt(b"{}")
    token_hash = cipher.encrypt(b"token")
    async with session_factory() as session:
        result = await session.execute(
            text(
                "INSERT INTO whatsapp_connections "
                "(user_id, provider, provider_connection_id, credentials, "
                "webhook_token_hash, connection_status) "
                "VALUES (:uid, 'baileys', :pid, :creds, :token, :status) "
                "RETURNING id"
            ),
            {
                "uid": user_id,
                "pid": f"conn-{user_id[:8]}",
                "creds": creds,
                "token": token_hash,
                "status": status,
            },
        )
        conn_id = str(result.scalar_one())
        await session.commit()
    return conn_id


async def _insert_message(
    session_factory: async_sessionmaker,
    *,
    user_id: str,
    connection_id: str,
    chat_id: str = "chat-1@c.us",
    timestamp: datetime | None = None,
) -> str:
    async with session_factory() as session:
        ts = timestamp or datetime.now(timezone.utc)
        result = await session.execute(
            text(
                "INSERT INTO messages "
                "(user_id, connection_id, chat_id, provider_message_id, "
                "direction, timestamp, message_type, text) "
                "VALUES (:uid, :cid, :chat, :pid, 'inbound', :ts, 'text', 'hello') "
                "RETURNING id"
            ),
            {
                "uid": user_id,
                "cid": connection_id,
                "chat": chat_id,
                "pid": f"msg-{ts.isoformat()}",
                "ts": ts,
            },
        )
        msg_id = str(result.scalar_one())
        await session.commit()
    return msg_id


async def test_run_once_rebuilds_recent_days(session_factory, clean_db) -> None:
    """run_once rebuilds daily stats for monitored children."""
    child_id = await insert_user(session_factory, "+9725000001")
    guardian_id = await insert_user(session_factory, "+9725000002")
    await _insert_guardian_link(
        session_factory, guardian_user_id=guardian_id, child_user_id=child_id
    )
    conn_id = await _insert_connection(session_factory, child_id, "connected")

    # Insert a message yesterday
    tz = ZoneInfo("Asia/Jerusalem")
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    day_start_utc = datetime.combine(yesterday, datetime.min.time(), tz)
    await _insert_message(
        session_factory,
        user_id=child_id,
        connection_id=conn_id,
        timestamp=day_start_utc + timedelta(hours=2),
    )

    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
        config=ReportingConfig(default_timezone="Asia/Jerusalem"),
    )
    worker = DailyStatsWorker(
        session_factory=session_factory,
        reporting_service=service,
        daily_stats_repo=repo,
        default_timezone="Asia/Jerusalem",
    )

    now = datetime.now(timezone.utc)
    rebuilt = await worker.run_once(now_utc=now)

    # Should have rebuilt at least 1 day (yesterday)
    assert rebuilt >= 1

    # Verify the stats row exists
    stats = await repo.get(child_id, yesterday)
    assert stats is not None
    assert stats.messages_received == 1


async def test_run_once_no_children_returns_zero(session_factory, clean_db) -> None:
    """run_once with no monitored children returns 0."""
    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
    )
    worker = DailyStatsWorker(
        session_factory=session_factory,
        reporting_service=service,
        daily_stats_repo=repo,
    )

    rebuilt = await worker.run_once()
    assert rebuilt == 0


async def test_lock_key_is_stable() -> None:
    """The advisory lock key is deterministic for the same child+date."""
    repo = PostgresGuardDailyStatsRepository.__new__(PostgresGuardDailyStatsRepository)
    service = GuardReportingService.__new__(GuardReportingService)
    worker = DailyStatsWorker(
        session_factory=None,  # type: ignore[arg-type]
        reporting_service=service,  # type: ignore[arg-type]
        daily_stats_repo=repo,  # type: ignore[arg-type]
    )

    key1 = worker._lock_key("child-1", date(2026, 10, 25))
    key2 = worker._lock_key("child-1", date(2026, 10, 25))
    key3 = worker._lock_key("child-2", date(2026, 10, 25))

    assert key1 == key2  # same child+date → same key
    assert key1 != key3  # different child → different key


async def test_dates_to_rebuild_excludes_today(session_factory, clean_db) -> None:
    """dates_to_rebuild excludes today unless it's late enough."""
    repo = PostgresGuardDailyStatsRepository.__new__(PostgresGuardDailyStatsRepository)
    service = GuardReportingService.__new__(GuardReportingService)
    worker = DailyStatsWorker(
        session_factory=None,  # type: ignore[arg-type]
        reporting_service=service,  # type: ignore[arg-type]
        daily_stats_repo=repo,  # type: ignore[arg-type]
        lookback_days=3,
    )

    # Morning — today excluded
    morning = datetime(2026, 10, 25, 10, 0, 0, tzinfo=timezone.utc)
    dates = worker._dates_to_rebuild(morning)
    assert date(2026, 10, 25) not in dates
    assert date(2026, 10, 24) in dates
    assert len(dates) == 3

    # Late night — today included
    late = datetime(2026, 10, 25, 23, 30, 0, tzinfo=timezone.utc)
    dates_late = worker._dates_to_rebuild(late)
    assert date(2026, 10, 25) in dates_late
    assert len(dates_late) == 4


async def test_run_once_continues_on_per_child_error(
    session_factory, clean_db, monkeypatch
) -> None:
    """run_once continues to the next child when one fails."""
    child1 = await insert_user(session_factory, "+9725000001")
    child2 = await insert_user(session_factory, "+9725000002")
    guardian1 = await insert_user(session_factory, "+9725000003")
    guardian2 = await insert_user(session_factory, "+9725000004")
    await _insert_guardian_link(
        session_factory, guardian_user_id=guardian1, child_user_id=child1
    )
    await _insert_guardian_link(
        session_factory, guardian_user_id=guardian2, child_user_id=child2
    )
    await _insert_connection(session_factory, child1, "connected")
    await _insert_connection(session_factory, child2, "connected")

    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
        config=ReportingConfig(default_timezone="Asia/Jerusalem"),
    )
    worker = DailyStatsWorker(
        session_factory=session_factory,
        reporting_service=service,
        daily_stats_repo=repo,
        default_timezone="Asia/Jerusalem",
    )

    # Make rebuild_day fail for child1 but succeed for child2.
    original_rebuild = service.rebuild_day
    call_count = [0]

    async def flaky_rebuild(*, child_user_id, local_date, timezone_name=None):
        call_count[0] += 1
        if child_user_id == child1:
            raise RuntimeError("simulated failure for child1")
        return await original_rebuild(
            child_user_id=child_user_id,
            local_date=local_date,
            timezone_name=timezone_name,
        )

    monkeypatch.setattr(service, "rebuild_day", flaky_rebuild)

    now = datetime.now(timezone.utc)
    rebuilt = await worker.run_once(now_utc=now)

    # child1 failed but child2 succeeded — at least 1 rebuilt
    assert rebuilt >= 1


async def test_run_loop_handles_sweep_exception(
    session_factory, clean_db, monkeypatch
) -> None:
    """run_loop continues after a sweep-level exception."""
    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
    )
    worker = DailyStatsWorker(
        session_factory=session_factory,
        reporting_service=service,
        daily_stats_repo=repo,
        poll_interval_seconds=0.01,
    )

    call_count = [0]

    async def failing_run_once(*, now_utc=None):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("sweep failure")
        # Second call stops the loop via cancellation
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "run_once", failing_run_once)

    # The loop should survive the first exception and then exit on cancellation.
    with pytest.raises(asyncio.CancelledError):
        await worker.run_loop()
    assert call_count[0] == 2


async def test_try_lock_and_rebuild_skips_when_locked(
    session_factory, clean_db, monkeypatch
) -> None:
    """_try_lock_and_rebuild returns False when the lock is not acquired."""
    child_id = await insert_user(session_factory, "+9725000001")
    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
    )
    worker = DailyStatsWorker(
        session_factory=session_factory,
        reporting_service=service,
        daily_stats_repo=repo,
    )

    # Simulate pg_try_advisory_xact_lock returning False (lock held by another worker).
    class _MockResult:
        def scalar(self) -> bool:
            return False

    original_execute = AsyncSession.execute

    async def mock_execute(self_session, statement, *args, **kwargs):
        # Return False for the advisory lock call, delegate for others.
        if "pg_try_advisory_xact_lock" in str(statement):
            return _MockResult()
        return await original_execute(self_session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", mock_execute)

    acquired = await worker._try_lock_and_rebuild(
        child_id=child_id, local_date=date(2026, 10, 25)
    )
    assert acquired is False
