"""Postgres-backed Guard persistence tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from echo_v2.domain.chat import Message
from echo_v2.domain.guard import GuardAnalysisRecord
from echo_v2.domain.guard_operations import (
    GuardAnalysisAttempt,
    GuardAnalysisAttemptStatus,
)
from echo_v2.persistence.chat_repositories import AnalysisTarget
from echo_v2.persistence.guard_operations import (
    PostgresGuardAnalysisTelemetryRepository,
)
from echo_v2.persistence.postgres_chat import (
    PostgresIngestionRepository,
    PostgresMessageRepository,
)
from echo_v2.persistence.postgres_guard import (
    PostgresGuardAnalysisCommitRepository,
    PostgresGuardAnalysisRepository,
    PostgresGuardChatStateRepository,
    PostgresGuardianChildLinkRepository,
)
from echo_v2.ports.whatsapp import MessageDirection
from echo_v2.services.guard_taxonomy import GuardDecision
from tests.persistence.conftest import insert_user

pytestmark = pytest.mark.asyncio


async def _connection(session_factory, user_id: str) -> str:
    from sqlalchemy import text

    async with session_factory() as session:
        result = await session.execute(
            text(
                "INSERT INTO whatsapp_connections "
                "(user_id, provider, provider_connection_id, credentials, "
                "webhook_token_hash, connection_status) "
                "VALUES (:uid, 'baileys', :pid, :creds, :hash, 'connected') RETURNING id"
            ),
            {"uid": user_id, "pid": str(uuid.uuid4()), "creds": b"x", "hash": b"x"},
        )
        value = str(result.scalar_one())
        await session.commit()
        return value


async def test_guard_link_and_state_repositories(session_factory, clean_db) -> None:
    guardian = await insert_user(session_factory, "+972546610653")
    child = await insert_user(session_factory, "+972546610654")
    now = datetime.now(timezone.utc)
    links = PostgresGuardianChildLinkRepository(session_factory)
    await links.upsert_active(guardian_user_id=guardian, child_user_id=child, now=now)
    link = await links.get_active_for_child(child)
    assert link is not None and link.guardian_user_id == guardian

    states = PostgresGuardChatStateRepository(session_factory)
    state = await states.upsert_on_message(
        child_user_id=child, chat_id="group@g.us", observed_at=now,
        next_analysis_at=now + timedelta(seconds=120), pending_since=now,
        chat_name="Group", is_group=True,
    )
    assert state.activity_version == 1
    await states.upsert_on_message(
        child_user_id=child, chat_id="group@g.us", observed_at=now + timedelta(seconds=1),
        next_analysis_at=now + timedelta(seconds=121), pending_since=now + timedelta(seconds=1),
        chat_name="Group", is_group=True,
    )
    current = await states.get(child, "group@g.us")
    assert current is not None
    assert current.activity_version == 2
    assert current.next_analysis_at == now + timedelta(seconds=120)
    assert (await states.list_for_children([child]))[0].chat_id == "group@g.us"


async def test_active_pair_requires_guardian_authorization_and_safety(session_factory, clean_db) -> None:
    guardian = await insert_user(session_factory, "+972546610656")
    child = await insert_user(session_factory, "+972546610657")
    links = PostgresGuardianChildLinkRepository(session_factory)
    async with session_factory() as session:
        await session.execute(
            text("""INSERT INTO guardian_child_links(
                guardian_user_id, child_user_id, status, safety_enabled_at
            ) VALUES (:guardian, :child, 'active', now())"""),
            {"guardian": guardian, "child": child},
        )
        await session.commit()
    assert await links.get_active_for_pair(guardian_user_id=guardian, child_user_id=child) is None
    async with session_factory() as session:
        await session.execute(
            text("""UPDATE guardian_child_links SET guardian_authorized_at = now(),
                   safety_enabled_at = NULL WHERE guardian_user_id = :guardian"""),
            {"guardian": guardian},
        )
        await session.commit()
    assert await links.get_active_for_pair(guardian_user_id=guardian, child_user_id=child) is None
    async with session_factory() as session:
        await session.execute(
            text("""UPDATE guardian_child_links SET safety_enabled_at = now()
                   WHERE guardian_user_id = :guardian"""),
            {"guardian": guardian},
        )
        await session.commit()
    assert await links.get_active_for_pair(guardian_user_id=guardian, child_user_id=child) is not None


async def test_unverified_reconnect_pauses_guard_without_revoking_parent(session_factory, clean_db) -> None:
    guardian = await insert_user(session_factory, "+972546610658")
    child = await insert_user(session_factory, "+972546610659")
    links = PostgresGuardianChildLinkRepository(session_factory)
    await links.upsert_active(guardian_user_id=guardian, child_user_id=child, now=datetime.now(timezone.utc))
    connection_id = await _connection(session_factory, child)
    async with session_factory() as session:
        await session.execute(
            text("UPDATE whatsapp_connections SET auth_loss_episode_key='episode-1' WHERE id=:id"),
            {"id": connection_id},
        )
        await session.commit()
    assert await links.get_active_for_child(child) is None
    assert await links.get_active_for_pair(guardian_user_id=guardian, child_user_id=child) is not None
    async with session_factory() as session:
        await session.execute(
            text("UPDATE whatsapp_connections SET auth_loss_episode_key=NULL WHERE id=:id"),
            {"id": connection_id},
        )
        await session.commit()
    assert await links.get_active_for_child(child) is not None


async def test_guard_ingestion_and_version_fenced_commit(session_factory, clean_db) -> None:
    child = await insert_user(session_factory, "+972546610655")
    connection_id = await _connection(session_factory, child)
    ingestion = PostgresIngestionRepository(session_factory)
    message = Message(
        id=str(uuid.uuid4()), user_id=child, connection_id=connection_id,
        chat_id="chat@c.us", provider_message_id="msg-1",
        direction=MessageDirection.INBOUND, sender_id="sender",
        timestamp=datetime.now(timezone.utc), text="hello",
    )
    assert await ingestion.ingest_if_new(
        message=message, direction=message.direction, observed_at=message.timestamp,
        next_analysis_at=message.timestamp, analysis_target=AnalysisTarget.GUARD,
    )
    assert not await ingestion.ingest_if_new(
        message=message, direction=message.direction, observed_at=message.timestamp,
        next_analysis_at=message.timestamp, analysis_target=AnalysisTarget.GUARD,
    )
    record = GuardAnalysisRecord(
        child_user_id=child, connection_id=connection_id, chat_id=message.chat_id,
        target_version=1, signals=(), categories=(), confidence=1.0, reason="none",
        evidence_message_ids=(), decision=GuardDecision.NONE, model="test",
        prompt_version="test", analyzer_version="test", taxonomy_version="test",
    )
    commit = PostgresGuardAnalysisCommitRepository(session_factory)
    status, result_id = await commit.commit_if_current(
        child_user_id=child, chat_id=message.chat_id, target_version=1, record=record
    )
    assert status == "committed"
    assert result_id is not None
    state = await PostgresGuardChatStateRepository(session_factory).get(child, message.chat_id)
    assert state is not None and state.last_analyzed_version == 1
    duplicate_status, _ = await commit.commit_if_current(
        child_user_id=child, chat_id=message.chat_id, target_version=1, record=record
    )
    assert duplicate_status == "committed"
    newer = Message(
        id=str(uuid.uuid4()), user_id=child, connection_id=connection_id,
        chat_id=message.chat_id, provider_message_id="msg-2",
        direction=MessageDirection.INBOUND, sender_id="sender",
        timestamp=message.timestamp + timedelta(seconds=1), text="new",
    )
    assert await ingestion.ingest_if_new(
        message=newer, direction=newer.direction, observed_at=newer.timestamp,
        next_analysis_at=newer.timestamp, analysis_target=AnalysisTarget.GUARD,
    )
    stale_status, _ = await commit.commit_if_current(
        child_user_id=child, chat_id=message.chat_id, target_version=1, record=record
    )
    assert stale_status == "stale"


async def test_message_repository_resolves_old_evidence_message(
    session_factory, clean_db
) -> None:
    child = await insert_user(session_factory, "+972546610658")
    connection_id = await _connection(session_factory, child)
    messages = PostgresMessageRepository(session_factory)
    now = datetime.now(timezone.utc)
    evidence = Message(
        id=str(uuid.uuid4()), user_id=child, connection_id=connection_id,
        chat_id="chat@c.us", provider_message_id="evidence-message",
        direction=MessageDirection.INBOUND, sender_id="sender", timestamp=now,
        text="old evidence",
    )
    assert await messages.save(evidence)
    for index in range(100):
        recent = Message(
            id=str(uuid.uuid4()), user_id=child, connection_id=connection_id,
            chat_id="chat@c.us", provider_message_id=f"recent-{index}",
            direction=MessageDirection.INBOUND, sender_id="sender",
            timestamp=now + timedelta(seconds=index + 1), text="recent message",
        )
        assert await messages.save(recent)

    recent_messages = await messages.list_recent_for_chat(
        user_id=child, chat_id="chat@c.us", limit=100
    )
    evidence_messages = await messages.list_by_ids(
        user_id=child, chat_id="chat@c.us", message_ids={evidence.id}
    )

    assert evidence not in recent_messages
    assert [message.text for message in evidence_messages] == ["old evidence"]


async def test_guard_observation_replay_and_due_state(session_factory, clean_db) -> None:
    child = await insert_user(session_factory, "+972546610656")
    connection_id = await _connection(session_factory, child)
    state_repo = PostgresGuardChatStateRepository(session_factory)
    now = datetime.now(timezone.utc)
    await state_repo.upsert_on_message(
        child_user_id=child, chat_id="chat@c.us", observed_at=now,
        next_analysis_at=now - timedelta(seconds=1), pending_since=now,
        chat_name=None, is_group=False,
    )
    analysis_repo = PostgresGuardAnalysisRepository(session_factory)
    record = GuardAnalysisRecord(
        child_user_id=child, connection_id=connection_id, chat_id="chat@c.us",
        target_version=1, signals=("watch_signal",), categories=(), confidence=0.5,
        reason="watch", evidence_message_ids=(), decision=GuardDecision.WATCH,
        model="test", prompt_version="p", analyzer_version="a", taxonomy_version="t",
    )
    result_id = await analysis_repo.save(record)
    assert result_id
    listed = await analysis_repo.list_for_chat(
        child_user_id=child, chat_id="chat@c.us"
    )
    assert listed[0].id == result_id
    replay = await analysis_repo.list_for_replay(
        child_user_id=child, chat_id="chat@c.us", since=now - timedelta(hours=1),
        before_version=2,
    )
    assert len(replay) == 1
    assert (await state_repo.list_due(now))[0].chat_id == "chat@c.us"
    assert await state_repo.mark_processed(
        child, "chat@c.us", 1, decision=GuardDecision.WATCH, analyzed_at=now
    )
    assert await state_repo.list_due(now) == []


async def test_guard_analysis_telemetry_lifecycle(session_factory, clean_db) -> None:
    child = await insert_user(session_factory, "+972546610657")
    telemetry = PostgresGuardAnalysisTelemetryRepository(session_factory)
    started = datetime.now(timezone.utc)
    attempt = GuardAnalysisAttempt(
        id=str(uuid.uuid4()),
        child_user_id=child,
        chat_id="chat@g.us",
        target_version=3,
        status=GuardAnalysisAttemptStatus.STARTED,
        started_at=started,
        scheduled_for=started,
        pending_since=started - timedelta(seconds=10),
    )
    await telemetry.start(attempt)
    await telemetry.finish(
        attempt.id,
        status=GuardAnalysisAttemptStatus.FAILED,
        finished_at=started + timedelta(seconds=2),
        error_code="AnalysisError",
    )

    rows = await telemetry.list_for_chat(child_user_id=child, chat_id="chat@g.us")

    assert len(rows) == 1
    assert rows[0].status is GuardAnalysisAttemptStatus.FAILED
    assert rows[0].error_code == "AnalysisError"
    assert rows[0].target_version == 3
