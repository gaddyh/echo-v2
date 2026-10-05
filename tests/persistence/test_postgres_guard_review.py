from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.guard import GuardAnalysisRecord
from echo_v2.domain.guard_feedback import GuardFeedbackLabel
from echo_v2.persistence.postgres_guard import (
    PostgresGuardAnalysisRepository,
    PostgresGuardianChildLinkRepository,
)
from echo_v2.persistence.postgres_guard_feedback import PostgresGuardFeedbackRepository
from echo_v2.services.guard_taxonomy import GuardDecision
from tests.persistence.conftest import insert_user

pytestmark = pytest.mark.asyncio


async def test_guard_review_queries_and_atomic_feedback(session_factory, clean_db) -> None:
    guardian = await insert_user(session_factory, "+972546610657")
    child = await insert_user(session_factory, "+972546610658")
    now = datetime.now(timezone.utc)
    links = PostgresGuardianChildLinkRepository(session_factory)
    await links.upsert_active(guardian_user_id=guardian, child_user_id=child, now=now)
    assert [link.child_user_id for link in await links.list_active_for_guardian(guardian)] == [child]
    # Use a valid existing connection row from the test database.
    from tests.persistence.test_postgres_guard import _connection
    connection_id = await _connection(session_factory, child)
    record = GuardAnalysisRecord(
        child_user_id=child, connection_id=connection_id, chat_id="review@c.us",
        target_version=1, signals=("signal",), categories=("category",), confidence=0.8,
        reason="reason", evidence_message_ids=("message",), decision=GuardDecision.WATCH,
        model="model", prompt_version="prompt", analyzer_version="analyzer", taxonomy_version="taxonomy",
        schedule_reason="none_with_new_activity", pending_since=now - timedelta(seconds=60),
        scheduled_for=now - timedelta(seconds=10),
    )
    analysis = PostgresGuardAnalysisRepository(session_factory)
    result_id = await analysis.save(record)
    rows = await analysis.list_for_children(child_user_ids=[child])
    assert rows[0].id == result_id
    assert rows[0].schedule_reason == "none_with_new_activity"
    feedback = PostgresGuardFeedbackRepository(session_factory)
    current = await feedback.set_feedback(
        result_id=result_id, reviewer_user_id=guardian,
        label=GuardFeedbackLabel.FALSE_POSITIVE, note="first",
    )
    assert current is not None
    current = await feedback.set_feedback(
        result_id=result_id, reviewer_user_id=guardian,
        label=GuardFeedbackLabel.CORRECT, note="changed",
    )
    assert current is not None and current.label == GuardFeedbackLabel.CORRECT
    events = await feedback.list_events(result_id)
    assert len(events) == 2
    assert events[1].old_label == GuardFeedbackLabel.FALSE_POSITIVE
    assert (await feedback.get_for_results([result_id]))[result_id].note == "changed"
