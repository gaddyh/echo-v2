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


class NoPhoneUsers(FakeUsers):
    async def get_phone_by_id(self, user_id: str) -> str | None:
        return None


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


class FalseCancelScheduling(FakeScheduling):
    async def cancel(self, action_id: str, user_id: str) -> bool:
        return False


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


async def setup(*, enabled: bool = True, users: object | None = None, scheduling=None):
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=NOW)
    actions = InMemoryScheduledActionRepository()
    delivery = InMemoryExposureDeliveryRepository()
    scheduler = scheduling or FakeScheduling(actions)
    service = ExposureDeliveryService(
        links=links,
        users=users or FakeUsers(),
        security_assessments=InMemoryGuardSimpleAssessmentRepository(),
        delivery=delivery,
        actions=actions,
        scheduling=scheduler,
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
    outcome = await delivery.get_outcome("exposure-1", guardian_user_id="guardian")
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
    outcome = await delivery.get_outcome("exposure-1", guardian_user_id="guardian")
    assert action is not None and action.status is ScheduledActionStatus.CANCELLED
    assert outcome is not None and outcome.reason == "security_precedence"


async def test_sent_exposure_is_not_cancelled_and_different_evidence_is_allowed() -> None:
    service, delivery, actions = await setup()
    assert await service.evaluate_committed(
        assessment=exposure(assessment_id="sent"), assessment_id="sent"
    ) == "scheduled"
    sent = await delivery.get_outcome("sent", guardian_user_id="guardian")
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


async def test_security_history_and_duplicate_delivery_paths() -> None:
    service, delivery, actions = await setup()
    await service._security_assessments.save(security(evidence=("m1",)))
    assert await service.evaluate_committed(
        assessment=exposure(assessment_id="security-history"),
        assessment_id="security-history",
    ) == "security_precedence"
    assert await service.evaluate_committed(
        assessment=exposure(assessment_id="security-history"),
        assessment_id="security-history",
    ) == "security_precedence"
    assert await actions.list_pending("guardian") == []
    assert await service.validate_before_send({"exposure_assessment_id": "unknown", "guardian_user_id": "guardian"}) is False
    assert await delivery.get_outcome("security-history", guardian_user_id="guardian") is not None


async def test_missing_link_and_phone_are_not_scheduled() -> None:
    service, _delivery, actions = await setup()
    service._links._links.clear()
    assert await service.evaluate_committed(
        assessment=exposure(assessment_id="no-link"), assessment_id="no-link"
    ) == "no_active_link"
    assert await actions.list_pending("guardian") == []

    service, _delivery, actions = await setup(users=NoPhoneUsers())
    assert await service.evaluate_committed(
        assessment=exposure(assessment_id="no-phone"), assessment_id="no-phone"
    ) == "no_guardian_phone"
    assert await actions.list_pending("guardian") == []


async def test_state_reservation_prevents_a_racing_duplicate() -> None:
    service, delivery, actions = await setup()
    assert await service.evaluate_committed(
        assessment=exposure(assessment_id="first"), assessment_id="first"
    ) == "scheduled"
    delivery.outcomes.clear()
    assert await service.evaluate_committed(
        assessment=exposure(assessment_id="second"), assessment_id="second"
    ) == "duplicate_evidence"
    assert len(await actions.list_pending("guardian")) == 1


async def test_cancellation_ignores_missing_actions_and_disabled_security() -> None:
    service, delivery, _actions = await setup()
    assert await service.cancel_overlapping_security(replace(security(), parent_should_know=False)) == 0
    service._enabled = False
    assert await service.cancel_overlapping_security(security()) == 0
    service._enabled = True
    orphan = exposure(assessment_id="orphan")
    await delivery.save_outcome(
        replace(
            await service._ensure_outcome(orphan, "orphan", NOW, guardian_user_id="guardian"),
            status=ParentAttentionOutcomeStatus.SCHEDULED,
            reason="scheduled",
            scheduled_action_id="missing-action",
        )
    )
    assert await service.cancel_overlapping_security(security()) == 0
    assert await service.evaluate_committed(assessment=orphan, assessment_id="orphan") == "scheduled"


async def test_validation_and_failed_evaluation_paths() -> None:
    service, delivery, _actions = await setup(enabled=False)
    assert not await service.validate_before_send({})
    assert not await service.validate_before_send({"exposure_assessment_id": "missing", "guardian_user_id": "guardian"})
    await service.mark_evaluation_failed(assessment_id="missing", error="ignored", guardian_user_id="guardian")

    service._enabled = True
    assert await service.evaluate_committed(
        assessment=exposure(assessment_id="failure"), assessment_id="failure"
    ) == "scheduled"
    assert await service.validate_before_send({"exposure_assessment_id": "failure", "guardian_user_id": "guardian"})
    await service.mark_evaluation_failed(assessment_id="failure", error="provider failed", guardian_user_id="guardian")
    outcome = await delivery.get_outcome("failure", guardian_user_id="guardian")
    assert outcome is not None and outcome.status is ParentAttentionOutcomeStatus.FAILED
    assert not await service.validate_before_send({"exposure_assessment_id": "failure", "guardian_user_id": "guardian"})


async def test_cancellation_handles_missing_claimed_and_non_pending_actions() -> None:
    service, delivery, actions = await setup()
    base = exposure(assessment_id="base")
    assert await service.evaluate_committed(assessment=base, assessment_id="base") == "scheduled"
    outcome = await delivery.get_outcome("base", guardian_user_id="guardian")
    assert outcome is not None

    await delivery.save_outcome(replace(outcome, scheduled_action_id=None))
    assert await service.cancel_overlapping_security(security()) == 0
    await delivery.save_outcome(outcome)

    action = await actions.get("action-1")
    assert action is not None
    await actions.save(replace(action, status=ScheduledActionStatus.IN_PROGRESS))
    assert await service.cancel_overlapping_security(security()) == 1
    action = await actions.get("action-1")
    assert action is not None and action.status is ScheduledActionStatus.IN_PROGRESS

    await delivery.save_outcome(replace(outcome, status=ParentAttentionOutcomeStatus.SCHEDULED))
    await actions.save(replace(action, status=ScheduledActionStatus.SUCCEEDED))
    assert await service.cancel_overlapping_security(security()) == 0


async def test_pending_cancellation_can_lose_the_race() -> None:
    service, delivery, _actions = await setup(scheduling=FalseCancelScheduling(InMemoryScheduledActionRepository()))
    assert await service.evaluate_committed(
        assessment=exposure(assessment_id="race"), assessment_id="race"
    ) == "scheduled"
    assert await service.cancel_overlapping_security(security()) == 0
    outcome = await delivery.get_outcome("race", guardian_user_id="guardian")
    assert outcome is not None and outcome.status is ParentAttentionOutcomeStatus.SCHEDULED


async def test_reconcile_handles_missing_and_pending_actions() -> None:
    service, delivery, actions = await setup()
    missing = exposure(assessment_id="missing-action")
    await delivery.save_outcome(
        replace(
            await service._ensure_outcome(missing, "missing-action", NOW, guardian_user_id="guardian"),
            status=ParentAttentionOutcomeStatus.SCHEDULED,
            reason="scheduled",
        )
    )
    pending = exposure(assessment_id="pending-action", evidence=("m9",))
    assert await service.evaluate_committed(assessment=pending, assessment_id="pending-action") == "scheduled"
    assert await service.reconcile_pending(limit=10) == 1
    assert (await delivery.get_outcome("missing-action", guardian_user_id="guardian")).status is ParentAttentionOutcomeStatus.INDETERMINATE
    assert await actions.list_pending("guardian")


async def test_reconcile_updates_all_terminal_action_outcomes() -> None:
    service, delivery, actions = await setup()
    for index, status in enumerate(
        (
            ScheduledActionStatus.SUCCEEDED,
            ScheduledActionStatus.CANCELLED,
            ScheduledActionStatus.FAILED,
            ScheduledActionStatus.INDETERMINATE,
        ),
        start=1,
    ):
        assessment_id = f"reconcile-{index}"
        item = exposure(assessment_id=assessment_id, evidence=(f"m{index}",))
        assert await service.evaluate_committed(assessment=item, assessment_id=assessment_id) == "scheduled"
        outcome = await delivery.get_outcome(assessment_id, guardian_user_id="guardian")
        assert outcome is not None and outcome.scheduled_action_id is not None
        action = await actions.get(outcome.scheduled_action_id)
        assert action is not None
        result = {"provider_message_id": "provider"} if status is ScheduledActionStatus.SUCCEEDED else None
        await actions.save(
            replace(
                action,
                status=status,
                result=result,
                error="action failed" if status in {
                    ScheduledActionStatus.CANCELLED,
                    ScheduledActionStatus.FAILED,
                    ScheduledActionStatus.INDETERMINATE,
                } else None,
            )
        )
    assert await service.reconcile_pending(limit=10) == 4
    assert (await delivery.get_outcome("reconcile-1", guardian_user_id="guardian")).status is ParentAttentionOutcomeStatus.SENT
    assert (await delivery.get_outcome("reconcile-2", guardian_user_id="guardian")).status is ParentAttentionOutcomeStatus.NOT_SENT
    assert (await delivery.get_outcome("reconcile-3", guardian_user_id="guardian")).status is ParentAttentionOutcomeStatus.FAILED
    assert (await delivery.get_outcome("reconcile-4", guardian_user_id="guardian")).status is ParentAttentionOutcomeStatus.INDETERMINATE
