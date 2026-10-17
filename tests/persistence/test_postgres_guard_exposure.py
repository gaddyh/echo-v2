"""Postgres-backed tests for Exposure Guard shadow persistence."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from echo_v2.domain.guard_exposure import ExposureAssessment, ExposureCategory
from echo_v2.persistence.postgres_guard_exposure import (
    PostgresGuardExposureAssessmentRepository,
    _json_safe,
)
from tests.persistence.conftest import insert_user

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)


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


def assessment(child: str, connection_id: str, *, decision: bool | None) -> ExposureAssessment:
    return ExposureAssessment(
        significant_exposure=decision,
        category=ExposureCategory.BULLYING if decision else None,
        summary="Repeated targeting." if decision else "No significant event.",
        evidence_message_ids=("m1",) if decision else (),
        reason="Targeted humiliation." if decision else "Ordinary conversation.",
        input_message_ids=("m1", "m2"),
        input_content_hash="hash",
        child_user_id=child,
        connection_id=connection_id,
        chat_id="class@g.us",
        chat_type="group",
        chat_name="Class group",
        timezone="UTC",
        window_start=NOW.replace(hour=0),
        window_end=NOW,
        target_version=1,
        model="test",
        prompt_version="test",
        analyzer_version="test",
        created_at=NOW,
    )


async def test_exposure_json_safe_diagnostics() -> None:
    assert _json_safe({"time": NOW, "items": (NOW,)}) == {
        "time": NOW.isoformat(), "items": [NOW.isoformat()]
    }
    assert _json_safe("plain") == "plain"


async def test_exposure_repository_round_trip_and_queries(session_factory, clean_db) -> None:
    child = await insert_user(session_factory, "+972546610690")
    connection_id = await connection(session_factory, child)
    repository = PostgresGuardExposureAssessmentRepository(session_factory)

    saved_id = await repository.save(assessment(child, connection_id, decision=True))
    await repository.save(assessment(child, connection_id, decision=False))
    await repository.save(replace(assessment(child, connection_id, decision=None), id=str(uuid.uuid4())))
    loaded = await repository.get(saved_id)
    listed = await repository.list_for_children(
        child_user_ids=[child], chat_id="class@g.us", decision="yes", limit=10,
    )
    counts = await repository.count_for_children(child_user_ids=[child])
    assert await repository.get("00000000-0000-0000-0000-000000000000") is None
    assert await repository.list_for_children(child_user_ids=[], limit=10) == []
    assert await repository.list_for_children(child_user_ids=[child], decision="no", limit=10)
    assert await repository.list_for_children(child_user_ids=[child], decision="unknown", limit=10)
    await repository.list_for_children(
        child_user_ids=[child], before=(NOW, saved_id), limit=10,
        created_after=NOW.replace(hour=0), created_before=NOW.replace(hour=23),
    )

    assert loaded is not None and loaded.category is ExposureCategory.BULLYING
    assert listed[0].id == saved_id
    assert counts == {"analyzed": 3, "yes": 1, "no": 1, "unknown": 1}


async def test_exposure_commit_requires_current_guard_state(session_factory, clean_db) -> None:
    child = await insert_user(session_factory, "+972546610691")
    connection_id = await connection(session_factory, child)
    from sqlalchemy import text

    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO guard_chat_state "
                "(child_user_id, chat_id, activity_version, last_message_at, connection_id, is_group) "
                "VALUES (:child, :chat, 1, :at, :connection, true)"
            ),
            {"child": child, "chat": "class@g.us", "at": NOW, "connection": connection_id},
        )
        await session.commit()
    repository = PostgresGuardExposureAssessmentRepository(session_factory)
    status, item_id = await repository.commit_if_current(
        child_user_id=child, chat_id="class@g.us", target_version=1,
        assessment=assessment(child, connection_id, decision=True),
    )
    stale_status, stale_id = await repository.commit_if_current(
        child_user_id=child, chat_id="class@g.us", target_version=99,
        assessment=assessment(child, connection_id, decision=True),
    )
    assert (status, item_id) == ("committed", item_id)
    assert (stale_status, stale_id) == ("stale", None)
