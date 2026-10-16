"""Tests for Baseline B parent-attention delivery deduplication."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
)
from echo_v2.persistence.guard_repositories import InMemoryGuardianChildLinkRepository
from echo_v2.persistence.parent_attention_delivery import (
    InMemoryParentAttentionDeliveryRepository,
)
from echo_v2.services.parent_attention_delivery_service import (
    ParentAttentionDeliveryService,
)

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


class FakeUsers:
    async def get_phone_by_id(self, user_id: str) -> str | None:
        return "+972501234567"


class FakeScheduling:
    def __init__(self) -> None:
        self.actions: list[ScheduledAction] = []

    async def create_once(self, **kwargs):
        action = ScheduledAction(
            id=f"action-{len(self.actions) + 1}",
            user_id=kwargs["user_id"],
            type=kwargs["type"],
            execute_at_utc=kwargs["execute_at_utc"],
            timezone=kwargs["timezone_name"],
            status=ScheduledActionStatus.PENDING,
            payload=kwargs["payload"],
        )
        self.actions.append(action)
        return action, True


def assessment(*, evidence: tuple[str, ...], parent_should_know: bool | None) -> SafetyAssessment:
    return SafetyAssessment(
        parent_should_know=parent_should_know,
        child_role=ChildRole.TARGET,
        summary="Classmates repeatedly mock Shay.",
        reason="Ongoing humiliation directed at the child.",
        evidence_message_ids=evidence,
        child_user_id="child",
        child_name="Shay",
        connection_id="connection",
        chat_id="class@g.us",
        chat_type="group",
        chat_name="Class",
        created_at=NOW,
    )


async def test_false_and_unknown_do_not_schedule() -> None:
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=NOW)
    scheduling = FakeScheduling()
    service = ParentAttentionDeliveryService(
        links=links,
        users=FakeUsers(),
        delivery=InMemoryParentAttentionDeliveryRepository(),
        scheduling=scheduling,
    )

    assert await service.evaluate_committed(
        assessment=assessment(evidence=(), parent_should_know=False), assessment_id="a1"
    ) == "not_parent_attention"
    assert await service.evaluate_committed(
        assessment=assessment(evidence=(), parent_should_know=None), assessment_id="a2"
    ) == "not_parent_attention"
    assert scheduling.actions == []


async def test_sends_once_then_only_on_new_evidence_without_url_suffix() -> None:
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=NOW)
    delivery = InMemoryParentAttentionDeliveryRepository()
    scheduling = FakeScheduling()
    service = ParentAttentionDeliveryService(
        links=links,
        users=FakeUsers(),
        delivery=delivery,
        scheduling=scheduling,
    )

    assert await service.evaluate_committed(
        assessment=assessment(evidence=("m1",), parent_should_know=True), assessment_id="a1"
    ) == "scheduled"
    assert await service.evaluate_committed(
        assessment=assessment(evidence=("m1",), parent_should_know=True), assessment_id="a2"
    ) == "duplicate_evidence"
    assert await service.evaluate_committed(
        assessment=assessment(evidence=("m1", "m2"), parent_should_know=True), assessment_id="a3"
    ) == "scheduled"
    assert len(scheduling.actions) == 2
    template = scheduling.actions[0].payload["template"]
    assert template["body_params"] == ["Shay", "Classmates repeatedly mock Shay.", "Class"]
    assert "url_suffix" not in template
