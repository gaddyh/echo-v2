"""Tests for the in-memory Exposure repository contract."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from echo_v2.domain.guard_exposure import ExposureAssessment
from echo_v2.persistence.guard_exposure_repositories import (
    InMemoryGuardExposureAssessmentRepository,
)
from echo_v2.persistence.guard_repositories import InMemoryGuardChatStateRepository

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)


def item(decision: bool | None, item_id: str | None = None) -> ExposureAssessment:
    return ExposureAssessment(
        id=item_id,
        significant_exposure=decision,
        category=None,
        summary="summary",
        evidence_message_ids=(),
        reason="reason",
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        chat_type="group",
        timezone="UTC",
        window_start=NOW,
        window_end=NOW,
        target_version=1,
        created_at=NOW,
    )


async def test_in_memory_exposure_queries_and_decision_filters() -> None:
    repo = InMemoryGuardExposureAssessmentRepository()
    await repo.save(item(True, "yes"))
    await repo.save(item(False, "no"))
    await repo.save(item(None, "unknown"))
    assert (await repo.get("yes")) is not None
    assert len(await repo.list_for_children(child_user_ids=["child"], decision="yes")) == 1
    assert len(await repo.list_for_children(child_user_ids=["child"], decision="no")) == 1
    assert len(await repo.list_for_children(child_user_ids=["child"], decision="unknown")) == 1
    assert await repo.list_for_children(child_user_ids=["child"], limit=0) == []
    assert await repo.list_for_children(child_user_ids=["child"], before=(NOW, "unknown"))
    assert (await repo.count_for_children(child_user_ids=["child"])) == {
        "analyzed": 3, "yes": 1, "no": 1, "unknown": 1,
    }


async def test_in_memory_exposure_commit_fences_state() -> None:
    states = InMemoryGuardChatStateRepository()
    repo = InMemoryGuardExposureAssessmentRepository(states)
    stale, stale_id = await repo.commit_if_current(
        child_user_id="child", chat_id="group@g.us", target_version=1, assessment=item(True),
    )
    assert (stale, stale_id) == ("stale", None)
    await states.upsert_on_message(
        child_user_id="child", chat_id="group@g.us", observed_at=NOW,
        next_analysis_at=NOW, pending_since=NOW, connection_id="connection", is_group=True,
    )
    committed, item_id = await repo.commit_if_current(
        child_user_id="child", chat_id="group@g.us", target_version=1, assessment=item(True),
    )
    assert committed == "committed" and item_id is not None
