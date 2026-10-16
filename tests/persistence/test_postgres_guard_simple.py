"""Postgres-backed tests for isolated Baseline B assessment history."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.chat import Message
from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
from echo_v2.persistence.postgres_chat import PostgresMessageRepository
from echo_v2.persistence.postgres_guard_simple import (
    PostgresGuardSimpleAssessmentRepository,
)
from echo_v2.ports.whatsapp import MessageDirection
from tests.persistence.conftest import insert_user

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 16, 10, tzinfo=timezone.utc)


async def connection(session_factory, user_id: str) -> str:
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


def assessment(
    child: str, connection_id: str, *, parent_should_know: bool | None, created_at: datetime
) -> SafetyAssessment:
    return SafetyAssessment(
        child_user_id=child,
        connection_id=connection_id,
        chat_id="group@g.us",
        chat_type="group",
        timezone="Asia/Jerusalem",
        window_start=NOW.replace(hour=0),
        window_end=NOW,
        parent_should_know=parent_should_know,
        child_role=ChildRole.TARGET,
        summary="summary",
        reason="reason",
        evidence_message_ids=("message-1",) if parent_should_know else (),
        model="test",
        prompt_version="baseline-b-test",
        analyzer_version="baseline-b-test",
        created_at=created_at,
    )


async def test_assessments_are_isolated_and_resolution_is_separate(
    session_factory, clean_db
) -> None:
    child = await insert_user(session_factory, "+972546610690")
    connection_id = await connection(session_factory, child)
    repository = PostgresGuardSimpleAssessmentRepository(session_factory)

    result_id = await repository.save(
        assessment(child, connection_id, parent_should_know=True, created_at=NOW)
    )
    assert result_id
    await repository.save(
        assessment(
            child,
            connection_id,
            parent_should_know=False,
            created_at=NOW.replace(hour=11),
        )
    )

    current = await repository.latest_unresolved(child_user_id=child, chat_id="group@g.us")
    assert current is not None
    assert current.id == result_id
    assert current.parent_should_know is True

    assert await repository.resolve(
        assessment_id=result_id, resolved_by="guardian", resolved_at=NOW
    )
    assert await repository.latest_unresolved(
        child_user_id=child, chat_id="group@g.us"
    ) is None
    listed = await repository.list_for_chat(child_user_id=child, chat_id="group@g.us")
    assert len(listed) == 2
    assert listed[0].resolved_at == NOW
    assert listed[0].summary == "summary"


async def test_postgres_message_window_reports_bounds_connections_and_unknown_senders(
    session_factory, clean_db
) -> None:
    child = await insert_user(session_factory, "+972546610691")
    connection_id = await connection(session_factory, child)
    repository = PostgresMessageRepository(session_factory)
    await repository.save(
        Message(
            id=str(uuid.uuid4()),
            user_id=child,
            connection_id=connection_id,
            chat_id="chat@c.us",
            provider_message_id="window-1",
            direction=MessageDirection.INBOUND,
            sender_id=None,
            timestamp=NOW,
            text="unknown sender",
        )
    )

    window = await repository.list_for_chat_window(
        user_id=child,
        chat_id="chat@c.us",
        since=NOW.replace(hour=0),
        until=NOW + timedelta(microseconds=1),
        limit=100,
    )

    assert len(window.messages) == 1
    assert window.connection_ids == (connection_id,)
    assert window.unknown_sender_count == 1
