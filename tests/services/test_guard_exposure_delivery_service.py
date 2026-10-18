"""Tests for opt-in Exposure notification delivery and precedence."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from echo_v2.domain.guard_exposure import ExposureAssessment, ExposureCategory
from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
from echo_v2.domain.parent_attention import ParentAttentionOutcomeStatus
from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
)
from echo_v2.persistence.guard_exposure_delivery import (
    InMemoryExposureDeliveryRepository,
)
from echo_v2.persistence.guard_repositories import InMemoryGuardianChildLinkRepository
from echo_v2.persistence.guard_simple_repositories import (
    InMemoryGuardSimpleAssessmentRepository,
)
from echo_v2.persistence.scheduled_actions import InMemoryScheduledActionRepository
from echo_v2.services.guard_exposure_delivery_service import ExposureDeliveryService

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


class FakeUsers:
    async def get_phone_by_id(self, user_id: str) -> str | None:
        return "+972501234567"


class FakeScheduling:
    def __init__(self, actions: InMemoryScheduledActionRepository) -> None:
        self.actions = actions
        self.counter = 0

    async def create_once(self, **kwargs):
        self.counter += 1
        action = ScheduledAction(
            id=f"action-{self.counter}",
            user_id=kwargs["user_id"],
            type=kwargs["type"],
            execute_at_utc=kwargs["execute_at_utc"],
            timezone=kwargs["timezone_name"],
            status=ScheduledActionStatus.PENDING,
            payload=kwargs["payload"],
        )
        return await self.actions.create_once(action)

    async def cancel(self, action_id: str, user_id: str) -> bool:
        return await self.actions.cancel(action_id, user_id)


def exposure(*, evidence: tuple[str, ...] = ("m1",), assessment_id: str | None = None) -> ExposureAssessment:
    return ExposureAssessment(
        id=assessment_id,
        significant_exposure=True,
        category=ExposureCategory.BULLYING,
        summary="זוהתה השפלה חוזרת של אחד המשתתפים.",
        reason="קיימת השפלה חוזרת בקבוצה.",
        evidence_message_ids=evidence,
        child_user_id="child",
        chat_id="class@g.us",
        chat_name="הכיתה שלנו",
        created_at=NOW,
    )


def security(*, evidence: tuple[str, ...] = ("m1",), decision: bool | None = True) -> SafetyAssessment:
    return SafetyAssessment(
        parent_should_know=decision,
        child_role=ChildRole.TARGET,
        summary="זוהו איומים המופנים אל הילד.",
        reason="direct risk",
        evidence_message_ids=evidence,
        child_user_id="child",
        child_name="שי",
        chat_id="class@g.us",
        chat_type="group",
        chat_name="הכיתה שלנו",
        created_at=NOW,
    )


async def setup(*, enabled: bool = True):
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=NOW)
    actions = InMemoryScheduledActionRepository()
    delivery = InMemoryExposureDeliveryRepository()
    service = ExposureDeliveryService(
        links=links,
        users=FakeUsers(),
        security_assessments=InMemoryGuardSimpleAssessmentRepository(),
        delivery=delivery,
        actions=actions,
        scheduling=FakeScheduling(actions),
        enabled=enabled,
    )
    return service, delivery, actions


async def test_exposure_yes_schedules_calmer_template_without_url() -> None:
    service, delivery, actions = await setup()
    assert await service.evaluate_committed(
        assessment=exposure(assessment_id="exposure-1"), assessment_id="exposure-1"
    ) == "scheduled"
    pending = await actions.list_pending("guardian")
    assert len(pending) == 1
    assert pending[0].payload["kind"] == "guard_exposure_alert"
    assert pending[0].payload["template"] == {
        "name": "guard_exposure_alert_he",
        "language": "he",
        "body_params": ["הכיתה שלנו", "זוהתה השפלה חוזרת של אחד המשתתפים."],
    }
    outcome = await delivery.get_outcome("exposure-1")
    assert outcome is not None and outcome.status is ParentAttentionOutcomeStatus.SCHEDULED


async def test_disabled_unknown_and_non_exposure_do_not_schedule() -> None:
    service, _delivery, actions = await setup(enabled=False)
    assert await service.evaluate_committed(
        assessment=exposure(assessment_id="disabled"), assessment_id="disabled"
    ) == "disabled"
    service._enabled = True
    assert await service.evaluate_committed(
        assessment=replace(exposure(assessment_id="unknown"), significant_exposure=None),
        assessment_id="unknown",
    ) == "not_exposure"
    assert await service.evaluate_committed(
        assessment=replace(exposure(assessment_id="negative"), significant_exposure=False),
        assessment_id="negative",
    ) == "not_exposure"
    assert await actions.list_pending("guardian") == []


async def test_security_overlap_suppresses_exposure_and_cancels_pending_action() -> None:
    service, delivery, actions = await setup()
    exposure_item = exposure(assessment_id="exposure-1")
    assert await service.evaluate_committed(
        assessment=exposure_item, assessment_id="exposure-1"
    ) == "scheduled"
    assert await service.cancel_overlapping_security(security()) == 1
    action = await actions.get("action-1")
    outcome = await delivery.get_outcome("exposure-1")
    assert action is not None and action.status is ScheduledActionStatus.CANCELLED
    assert outcome is not None and outcome.reason == "security_precedence"


async def test_sent_exposure_is_not_cancelled_and_different_evidence_is_allowed() -> None:
    service, delivery, actions = await setup()
    assert await service.evaluate_committed(
        assessment=exposure(assessment_id="sent"), assessment_id="sent"
    ) == "scheduled"
    sent = await delivery.get_outcome("sent")
    assert sent is not None
    await delivery.save_outcome(
        replace(sent, status=ParentAttentionOutcomeStatus.SENT, reason="provider_confirmed")
    )
    assert await service.cancel_overlapping_security(security()) == 0
    assert (await actions.get("action-1")).status is ScheduledActionStatus.PENDING
    assert await service.evaluate_committed(
        assessment=exposure(evidence=("m2",), assessment_id="different"),
        assessment_id="different",
        security_assessment=security(evidence=("m1",)),
    ) == "scheduled"


async def test_exposure_duplicate_evidence_is_suppressed() -> None:
    service, _delivery, actions = await setup()
    assert await service.evaluate_committed(
        assessment=exposure(assessment_id="first"), assessment_id="first"
    ) == "scheduled"
    assert await service.evaluate_committed(
        assessment=exposure(assessment_id="second"), assessment_id="second"
    ) == "duplicate_evidence"
    assert len(await actions.list_pending("guardian")) == 1
