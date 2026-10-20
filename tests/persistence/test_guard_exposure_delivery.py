"""Tests for isolated Exposure delivery persistence contracts."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from echo_v2.domain.parent_attention import ParentAttentionOutcomeStatus
from echo_v2.persistence.guard_exposure_delivery import (
    ExposureDeliveryOutcome,
    ExposureDeliveryState,
    InMemoryExposureDeliveryRepository,
)

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
pytestmark = pytest.mark.asyncio


async def test_exposure_state_reservation_is_atomic_for_overlapping_evidence() -> None:
    repository = InMemoryExposureDeliveryRepository()
    first = ExposureDeliveryState(
        guardian_user_id="guardian",
        child_user_id="child",
        chat_id="group@g.us",
        last_scheduled_assessment_id="a1",
        last_scheduled_evidence_ids=("m1",),
        last_action_id="action-1",
        updated_at=NOW,
    )
    second = ExposureDeliveryState(
        guardian_user_id="guardian",
        child_user_id="child",
        chat_id="group@g.us",
        last_scheduled_assessment_id="a2",
        last_scheduled_evidence_ids=("m1", "m2"),
        last_action_id="action-2",
        updated_at=NOW,
    )
    assert await repository.reserve_state(first)
    assert not await repository.reserve_state(second)


async def test_exposure_outcome_tracks_evidence_and_status() -> None:
    repository = InMemoryExposureDeliveryRepository()
    outcome = ExposureDeliveryOutcome(
        assessment_id="a1",
        child_user_id="child",
        chat_id="group@g.us",
        evidence_message_ids=("m1",),
        status=ParentAttentionOutcomeStatus.SCHEDULED,
        reason="scheduled",
        guardian_user_id="guardian",
        scheduled_action_id="action-1",
        created_at=NOW,
        updated_at=NOW,
    )
    await repository.save_outcome(outcome)
    rows = await repository.list_overlapping(
        guardian_user_id="guardian",
        child_user_id="child",
        chat_id="group@g.us",
        evidence_message_ids=("m1", "m3"),
        pending_only=True,
    )
    assert rows == [outcome]
    assert await repository.get_outcome("a1", guardian_user_id="guardian") == outcome
