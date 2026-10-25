"""GuardReportingService tests — daily stats aggregation from existing records."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from echo_v2.domain.guard_daily_stats import CoverageStatus
from echo_v2.persistence.postgres_guard_daily_stats import (
    PostgresGuardDailyStatsRepository,
)
from echo_v2.services.reporting.guard_reporting_service import (
    GuardReportingService,
    ReportingConfig,
)
from tests.persistence.conftest import insert_user

pytestmark = pytest.mark.asyncio

BASE_TIME = datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc)


async def _insert_connection(
    session_factory: async_sessionmaker,
    user_id: str,
    status: str = "connected",
) -> str:
    """Insert a whatsapp_connections row and return its id."""
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
    chat_id: str,
    timestamp: datetime,
    message_type: str = "text",
    direction: str = "inbound",
    msg_text: str | None = "hello",
) -> str:
    """Insert a messages row and return its id."""
    async with session_factory() as session:
        result = await session.execute(
            text(
                "INSERT INTO messages "
                "(user_id, connection_id, chat_id, provider_message_id, "
                "direction, timestamp, message_type, text) "
                "VALUES (:uid, :cid, :chat, :pid, :dir, :ts, :mt, :txt) "
                "RETURNING id"
            ),
            {
                "uid": user_id,
                "cid": connection_id,
                "chat": chat_id,
                "pid": f"msg-{timestamp.isoformat()}-{chat_id}",
                "dir": direction,
                "ts": timestamp,
                "mt": message_type,
                "txt": msg_text,
            },
        )
        msg_id = str(result.scalar_one())
        await session.commit()
    return msg_id


async def _insert_enrichment(
    session_factory: async_sessionmaker,
    *,
    message_id: str,
    kind: str = "image",
    status: str = "completed",
    version: str = "2026-10-23.image.1",
) -> None:
    """Insert a message_enrichments row."""
    from uuid import uuid4

    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO message_enrichments "
                "(id, message_id, enrichment_version, status, kind, text, attempts) "
                "VALUES (:id, :mid, :ver, :status, :kind, :txt, 1)"
            ),
            {
                "id": str(uuid4()),
                "mid": message_id,
                "ver": version,
                "status": status,
                "kind": kind,
                "txt": "enriched text",
            },
        )
        await session.commit()


async def _insert_security_assessment(
    session_factory: async_sessionmaker,
    *,
    child_user_id: str,
    connection_id: str,
    chat_id: str = "chat-1@c.us",
    parent_should_know: bool = False,
    created_at: datetime = BASE_TIME,
) -> str:
    """Insert a guard_simple_assessments row."""
    async with session_factory() as session:
        result = await session.execute(
            text(
                "INSERT INTO guard_simple_assessments "
                "(child_user_id, connection_id, chat_id, chat_type, chat_name, "
                "timezone, window_start, window_end, parent_should_know, "
                "child_role, summary, reason, evidence_message_ids, "
                "input_message_ids, model, prompt_version, analyzer_version, "
                "created_at) "
                "VALUES (:cid, :conn, :chat, 'private', 'Test', 'Asia/Jerusalem', "
                ":ws, :we, :psk, 'target', 'summary', 'reason', '[]', '[]', "
                "'gpt-4.1', 'v1', 'v1', :created_at) RETURNING id"
            ),
            {
                "cid": child_user_id,
                "conn": connection_id,
                "chat": chat_id,
                "ws": created_at - timedelta(hours=1),
                "we": created_at,
                "psk": parent_should_know,
                "created_at": created_at,
            },
        )
        assessment_id = str(result.scalar_one())
        await session.commit()
    return assessment_id


async def _insert_security_outcome(
    session_factory: async_sessionmaker,
    *,
    assessment_id: str,
    child_user_id: str,
    guardian_user_id: str,
    status: str = "sent",
    created_at: datetime = BASE_TIME,
) -> None:
    """Insert a guard_parent_attention_delivery_outcomes row."""
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO guard_parent_attention_delivery_outcomes "
                "(assessment_id, guardian_user_id, child_user_id, chat_id, "
                "status, reason, created_at) "
                "VALUES (:aid, :gid, :cid, 'chat-1@c.us', :status, 'reason', :created_at)"
            ),
            {
                "aid": assessment_id,
                "gid": guardian_user_id,
                "cid": child_user_id,
                "status": status,
                "created_at": created_at,
            },
        )
        await session.commit()


async def _insert_guardian_link(
    session_factory: async_sessionmaker,
    *,
    guardian_user_id: str,
    child_user_id: str,
) -> None:
    """Insert a guardian_child_links row."""
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


async def test_rebuild_day_with_messages(session_factory, clean_db) -> None:
    """Rebuilding a day with messages produces correct counts."""
    child_id = await insert_user(session_factory, "+9725000001")
    guardian_id = await insert_user(session_factory, "+9725000002")
    await _insert_guardian_link(
        session_factory, guardian_user_id=guardian_id, child_user_id=child_id
    )
    conn_id = await _insert_connection(session_factory, child_id, "connected")

    # Insert messages in the day (2026-10-25 in Asia/Jerusalem = 2026-10-24 21:00 UTC to 2026-10-25 21:00 UTC)
    # Use UTC times that fall within the local day
    tz = ZoneInfo("Asia/Jerusalem")
    local_day = date(2026, 10, 15)
    day_start_utc = datetime.combine(local_day, datetime.min.time(), tz)

    await _insert_message(
        session_factory, user_id=child_id, connection_id=conn_id,
        chat_id="private-1@c.us", timestamp=day_start_utc + timedelta(hours=2),
        message_type="text",
    )
    await _insert_message(
        session_factory, user_id=child_id, connection_id=conn_id,
        chat_id="group-1@g.us", timestamp=day_start_utc + timedelta(hours=3),
        message_type="image",
    )
    await _insert_message(
        session_factory, user_id=child_id, connection_id=conn_id,
        chat_id="group-1@g.us", timestamp=day_start_utc + timedelta(hours=4),
        message_type="audio",
    )

    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
        config=ReportingConfig(default_timezone="Asia/Jerusalem"),
    )

    stats = await service.rebuild_day(
        child_user_id=child_id, local_date=local_day
    )

    assert stats.child_user_id == child_id
    assert stats.local_date == local_day
    assert stats.messages_received == 3
    assert stats.private_messages == 1
    assert stats.group_messages == 2
    assert stats.active_private_chats == 1
    assert stats.active_group_chats == 1
    assert stats.images_received == 1
    assert stats.audio_received == 1
    assert stats.coverage_health is True
    assert stats.coverage_status == CoverageStatus.PROTECTED


async def test_rebuild_day_idempotent(session_factory, clean_db) -> None:
    """Rebuilding the same day twice yields identical counts."""
    child_id = await insert_user(session_factory, "+9725000001")
    conn_id = await _insert_connection(session_factory, child_id, "connected")

    tz = ZoneInfo("Asia/Jerusalem")
    local_day = date(2026, 10, 15)
    day_start_utc = datetime.combine(local_day, datetime.min.time(), tz)

    await _insert_message(
        session_factory, user_id=child_id, connection_id=conn_id,
        chat_id="chat-1@c.us", timestamp=day_start_utc + timedelta(hours=2),
    )

    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
    )

    stats1 = await service.rebuild_day(child_user_id=child_id, local_date=local_day)
    stats2 = await service.rebuild_day(child_user_id=child_id, local_date=local_day)

    assert stats1.messages_received == stats2.messages_received
    assert stats1.coverage_status == stats2.coverage_status

    # Only one row in the DB
    rows = await repo.list_for_range(
        child_user_id=child_id,
        date_from=local_day,
        date_to=local_day,
    )
    assert len(rows) == 1


async def test_rebuild_day_with_security_detection_and_alert(
    session_factory, clean_db
) -> None:
    """Security detection and sent alert are counted separately."""
    child_id = await insert_user(session_factory, "+9725000001")
    guardian_id = await insert_user(session_factory, "+9725000002")
    await _insert_guardian_link(
        session_factory, guardian_user_id=guardian_id, child_user_id=child_id
    )
    conn_id = await _insert_connection(session_factory, child_id, "connected")

    tz = ZoneInfo("Asia/Jerusalem")
    local_day = date(2026, 10, 15)
    day_start_utc = datetime.combine(local_day, datetime.min.time(), tz)

    # Insert a security assessment with parent_should_know=True
    assessment_id = await _insert_security_assessment(
        session_factory,
        child_user_id=child_id,
        connection_id=conn_id,
        parent_should_know=True,
        created_at=day_start_utc + timedelta(hours=2),
    )

    # Insert a delivery outcome with status=sent
    await _insert_security_outcome(
        session_factory,
        assessment_id=assessment_id,
        child_user_id=child_id,
        guardian_user_id=guardian_id,
        status="sent",
    )

    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
    )

    stats = await service.rebuild_day(child_user_id=child_id, local_date=local_day)

    assert stats.security_detections == 1
    assert stats.security_alerts_sent == 1
    assert stats.security_attention is True
    assert stats.coverage_status == CoverageStatus.ATTENTION


async def test_rebuild_day_disconnected_child_is_incomplete(
    session_factory, clean_db
) -> None:
    """A disconnected child can never be protected."""
    child_id = await insert_user(session_factory, "+9725000001")
    # Connection status is "blocked" — not connected
    conn_id = await _insert_connection(session_factory, child_id, "blocked")

    tz = ZoneInfo("Asia/Jerusalem")
    local_day = date(2026, 10, 15)
    day_start_utc = datetime.combine(local_day, datetime.min.time(), tz)

    await _insert_message(
        session_factory, user_id=child_id, connection_id=conn_id,
        chat_id="chat-1@c.us", timestamp=day_start_utc + timedelta(hours=2),
    )

    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
    )

    stats = await service.rebuild_day(child_user_id=child_id, local_date=local_day)

    assert stats.coverage_health is False
    assert stats.coverage_status == CoverageStatus.INCOMPLETE


async def test_rebuild_day_no_connection_is_incomplete(
    session_factory, clean_db
) -> None:
    """A child with no connection at all is incomplete."""
    child_id = await insert_user(session_factory, "+9725000001")

    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
    )

    stats = await service.rebuild_day(
        child_user_id=child_id, local_date=date(2026, 10, 15)
    )

    assert stats.coverage_health is False
    assert stats.coverage_status == CoverageStatus.INCOMPLETE
    assert stats.messages_received == 0


async def test_rebuild_day_quiet_day_is_protected(session_factory, clean_db) -> None:
    """A connected child with no messages is still protected."""
    child_id = await insert_user(session_factory, "+9725000001")
    await _insert_connection(session_factory, child_id, "connected")

    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
    )

    stats = await service.rebuild_day(
        child_user_id=child_id, local_date=date(2026, 10, 15)
    )

    assert stats.messages_received == 0
    assert stats.coverage_health is True
    assert stats.coverage_status == CoverageStatus.PROTECTED


async def test_rebuild_range(session_factory, clean_db) -> None:
    """Rebuild a range of dates."""
    child_id = await insert_user(session_factory, "+9725000001")
    await _insert_connection(session_factory, child_id, "connected")

    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
    )

    results = await service.rebuild_range(
        child_user_id=child_id,
        date_from=date(2026, 10, 13),
        date_to=date(2026, 10, 15),
    )

    assert len(results) == 3
    assert results[0].local_date == date(2026, 10, 13)
    assert results[2].local_date == date(2026, 10, 15)
    for r in results:
        assert r.coverage_status == CoverageStatus.PROTECTED


async def test_rebuild_day_with_enrichments(session_factory, clean_db) -> None:
    """Enriched media counts are tracked separately from received."""
    child_id = await insert_user(session_factory, "+9725000001")
    conn_id = await _insert_connection(session_factory, child_id, "connected")

    tz = ZoneInfo("Asia/Jerusalem")
    local_day = date(2026, 10, 15)
    day_start_utc = datetime.combine(local_day, datetime.min.time(), tz)

    # Insert an image message + completed enrichment
    msg_id = await _insert_message(
        session_factory, user_id=child_id, connection_id=conn_id,
        chat_id="chat-1@c.us", timestamp=day_start_utc + timedelta(hours=2),
        message_type="image",
    )
    await _insert_enrichment(
        session_factory, message_id=msg_id, kind="image", status="completed"
    )

    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
    )

    stats = await service.rebuild_day(child_user_id=child_id, local_date=local_day)

    assert stats.images_received == 1
    assert stats.images_enriched == 1


async def test_rebuild_day_detection_without_alert(
    session_factory, clean_db
) -> None:
    """A detection that was not delivered (not_sent) is counted as detection but not alert."""
    child_id = await insert_user(session_factory, "+9725000001")
    guardian_id = await insert_user(session_factory, "+9725000002")
    await _insert_guardian_link(
        session_factory, guardian_user_id=guardian_id, child_user_id=child_id
    )
    conn_id = await _insert_connection(session_factory, child_id, "connected")

    tz = ZoneInfo("Asia/Jerusalem")
    local_day = date(2026, 10, 15)
    day_start_utc = datetime.combine(local_day, datetime.min.time(), tz)

    assessment_id = await _insert_security_assessment(
        session_factory,
        child_user_id=child_id,
        connection_id=conn_id,
        parent_should_know=True,
        created_at=day_start_utc + timedelta(hours=2),
    )

    # Delivery outcome is "not_sent" — detected but not delivered
    await _insert_security_outcome(
        session_factory,
        assessment_id=assessment_id,
        child_user_id=child_id,
        guardian_user_id=guardian_id,
        status="not_sent",
    )

    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
    )

    stats = await service.rebuild_day(child_user_id=child_id, local_date=local_day)

    assert stats.security_detections == 1
    assert stats.security_alerts_sent == 0  # not sent!
    assert stats.security_attention is False
    assert stats.coverage_status == CoverageStatus.PROTECTED


async def test_rebuild_day_with_video_link_document_enrichments(
    session_factory, clean_db
) -> None:
    """Video, link, and document enrichments are counted correctly."""
    child_id = await insert_user(session_factory, "+9725000001")
    conn_id = await _insert_connection(session_factory, child_id, "connected")

    tz = ZoneInfo("Asia/Jerusalem")
    local_day = date(2026, 10, 15)
    day_start_utc = datetime.combine(local_day, datetime.min.time(), tz)

    # Insert a video message + video enrichment
    msg1 = await _insert_message(
        session_factory, user_id=child_id, connection_id=conn_id,
        chat_id="chat-1@c.us", timestamp=day_start_utc + timedelta(hours=1),
        message_type="video",
    )
    await _insert_enrichment(
        session_factory, message_id=msg1, kind="video", status="completed"
    )

    # Insert a text message (link) + link enrichment
    msg2 = await _insert_message(
        session_factory, user_id=child_id, connection_id=conn_id,
        chat_id="chat-1@c.us", timestamp=day_start_utc + timedelta(hours=2),
        message_type="text", msg_text="https://example.com",
    )
    await _insert_enrichment(
        session_factory, message_id=msg2, kind="link", status="completed"
    )

    # Insert a document message + document enrichment
    msg3 = await _insert_message(
        session_factory, user_id=child_id, connection_id=conn_id,
        chat_id="chat-1@c.us", timestamp=day_start_utc + timedelta(hours=3),
        message_type="document",
    )
    await _insert_enrichment(
        session_factory, message_id=msg3, kind="document", status="completed"
    )

    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
    )

    stats = await service.rebuild_day(child_user_id=child_id, local_date=local_day)

    assert stats.videos_received == 1
    assert stats.videos_enriched == 1
    assert stats.links_enriched == 1
    assert stats.documents_received == 1
    assert stats.documents_enriched == 1


async def test_rebuild_day_with_exposure_detection(session_factory, clean_db) -> None:
    """Exposure detection is counted from guard_exposure_assessments."""
    child_id = await insert_user(session_factory, "+9725000001")
    conn_id = await _insert_connection(session_factory, child_id, "connected")

    tz = ZoneInfo("Asia/Jerusalem")
    local_day = date(2026, 10, 15)
    day_start_utc = datetime.combine(local_day, datetime.min.time(), tz)

    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO guard_exposure_assessments "
                "(child_user_id, connection_id, chat_id, chat_type, chat_name, "
                "timezone, window_start, window_end, target_version, "
                "significant_exposure, category, summary, reason, "
                "evidence_message_ids, input_message_ids, model, "
                "prompt_version, analyzer_version, created_at) "
                "VALUES (:cid, :conn, 'group-1@g.us', 'group', 'Test', "
                "'Asia/Jerusalem', :ws, :we, 1, true, 'bullying', "
                "'summary', 'reason', '[]', '[]', 'gemini-2.0-flash', 'v1', 'v1', "
                ":created_at)"
            ),
            {
                "cid": child_id,
                "conn": conn_id,
                "ws": day_start_utc,
                "we": day_start_utc + timedelta(hours=1),
                "created_at": day_start_utc + timedelta(hours=2),
            },
        )
        await session.commit()

    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
    )

    stats = await service.rebuild_day(child_user_id=child_id, local_date=local_day)

    assert stats.exposure_detections == 1
    assert stats.guard_assessments == 1


async def test_rebuild_day_with_alert_failure(session_factory, clean_db) -> None:
    """Failed alert delivery is counted in alerts_failed."""
    child_id = await insert_user(session_factory, "+9725000001")
    guardian_id = await insert_user(session_factory, "+9725000002")
    await _insert_guardian_link(
        session_factory, guardian_user_id=guardian_id, child_user_id=child_id
    )
    conn_id = await _insert_connection(session_factory, child_id, "connected")

    tz = ZoneInfo("Asia/Jerusalem")
    local_day = date(2026, 10, 15)
    day_start_utc = datetime.combine(local_day, datetime.min.time(), tz)

    assessment_id = await _insert_security_assessment(
        session_factory,
        child_user_id=child_id,
        connection_id=conn_id,
        parent_should_know=True,
        created_at=day_start_utc + timedelta(hours=2),
    )
    await _insert_security_outcome(
        session_factory,
        assessment_id=assessment_id,
        child_user_id=child_id,
        guardian_user_id=guardian_id,
        status="failed",
    )

    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
    )

    stats = await service.rebuild_day(child_user_id=child_id, local_date=local_day)

    assert stats.security_detections == 1
    assert stats.security_alerts_sent == 0
    assert stats.alerts_failed == 1
    # A failed alert doesn't count as security_attention
    assert stats.security_attention is False
    assert stats.coverage_status == CoverageStatus.PROTECTED


async def test_rebuild_day_degraded_connection_is_protected(
    session_factory, clean_db
) -> None:
    """A degraded connection is still considered healthy."""
    child_id = await insert_user(session_factory, "+9725000001")
    await _insert_connection(session_factory, child_id, "degraded")

    repo = PostgresGuardDailyStatsRepository(session_factory)
    service = GuardReportingService(
        session_factory=session_factory,
        daily_stats_repo=repo,
    )

    stats = await service.rebuild_day(
        child_user_id=child_id, local_date=date(2026, 10, 15)
    )

    assert stats.coverage_health is True
    assert stats.coverage_status == CoverageStatus.PROTECTED
    assert stats.connection_seconds == 86400
