"""Tests for Baseline B parent-attention delivery outcomes."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
from echo_v2.domain.parent_attention import (
    ParentAttentionDeliveryOutcome,
    ParentAttentionOutcomeStatus,
)
from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.persistence.guard_repositories import InMemoryGuardianChildLinkRepository
from echo_v2.persistence.guard_simple_repositories import (
    InMemoryGuardSimpleAssessmentRepository,
)
from echo_v2.persistence.parent_attention_delivery import (
    InMemoryParentAttentionDeliveryOutcomeRepository,
    InMemoryParentAttentionDeliveryRepository,
)
from echo_v2.persistence.scheduled_actions import InMemoryScheduledActionRepository
from echo_v2.services.parent_attention_delivery_service import (
    ParentAttentionDeliveryService,
)

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


class FakeUsers:
    async def get_phone_by_id(self, user_id: str) -> str | None:
        return "+972501234567"


class NoPhoneUsers:
    async def get_phone_by_id(self, user_id: str) -> str | None:
        return None


class FailingScheduling:
    async def create_once(self, **kwargs):
        raise RuntimeError("delivery evaluation failed")


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


def service(
    *,
    links: InMemoryGuardianChildLinkRepository | None = None,
    users: object | None = None,
    enabled: bool = True,
    scheduling: FakeScheduling | None = None,
    outcomes: InMemoryParentAttentionDeliveryOutcomeRepository | None = None,
    assessments: InMemoryGuardSimpleAssessmentRepository | None = None,
    actions: InMemoryScheduledActionRepository | None = None,
) -> tuple[ParentAttentionDeliveryService, InMemoryParentAttentionDeliveryOutcomeRepository, InMemoryScheduledActionRepository]:
    return_outcomes = outcomes or InMemoryParentAttentionDeliveryOutcomeRepository()
    return_actions = actions or InMemoryScheduledActionRepository()
    result = ParentAttentionDeliveryService(
        links=links or InMemoryGuardianChildLinkRepository(),
        users=users or FakeUsers(),
        delivery=InMemoryParentAttentionDeliveryRepository(),
        outcomes=return_outcomes,
        assessments=assessments or InMemoryGuardSimpleAssessmentRepository(),
        actions=return_actions,
        scheduling=scheduling or FakeScheduling(),
        enabled=enabled,
    )
    return result, return_outcomes, return_actions


async def test_active_child_without_link_is_not_scheduled() -> None:
    delivery, outcomes, _ = service()

    assert await delivery.evaluate_committed(
        assessment=assessment(evidence=("m1",), parent_should_know=True), assessment_id="a1"
    ) == "no_active_link"
    outcome = await outcomes.get("a1")
    assert outcome is not None
    assert outcome.status is ParentAttentionOutcomeStatus.NOT_SENT
    assert outcome.reason == "no_active_link"


async def test_delivery_disabled_does_not_schedule() -> None:
    delivery, outcomes, _ = service(enabled=False)

    assert await delivery.evaluate_committed(
        assessment=assessment(evidence=("m1",), parent_should_know=True), assessment_id="a1"
    ) == "disabled"
    outcome = await outcomes.get("a1")
    assert outcome is not None and outcome.reason == "disabled"


async def test_false_and_unknown_do_not_schedule() -> None:
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=NOW)
    scheduling = FakeScheduling()
    delivery, outcomes, _ = service(links=links, scheduling=scheduling)

    assert await delivery.evaluate_committed(
        assessment=assessment(evidence=(), parent_should_know=False), assessment_id="a1"
    ) == "not_parent_attention"
    assert await delivery.evaluate_committed(
        assessment=assessment(evidence=(), parent_should_know=None), assessment_id="a2"
    ) == "not_parent_attention"
    assert scheduling.actions == []
    assert (await outcomes.get("a1")).reason == "not_parent_attention"
    assert (await outcomes.get("a2")).reason == "not_parent_attention"

    no_link_delivery, no_link_outcomes, _ = service(links=links, users=NoPhoneUsers())
    assert await no_link_delivery.evaluate_committed(
        assessment=assessment(evidence=("m1",), parent_should_know=True), assessment_id="no-phone"
    ) == "no_guardian_phone"
    assert (await no_link_outcomes.get("no-phone")).reason == "no_guardian_phone"


async def test_sends_once_then_only_on_new_evidence_without_url_suffix() -> None:
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=NOW)
    delivery_repo = InMemoryParentAttentionDeliveryRepository()
    outcomes = InMemoryParentAttentionDeliveryOutcomeRepository()
    scheduling = FakeScheduling()
    delivery = ParentAttentionDeliveryService(
        links=links,
        users=FakeUsers(),
        delivery=delivery_repo,
        outcomes=outcomes,
        assessments=InMemoryGuardSimpleAssessmentRepository(),
        actions=InMemoryScheduledActionRepository(),
        scheduling=scheduling,
        enabled=True,
    )

    assert await delivery.reconcile_pending(limit=3) == 0
    assert await delivery.evaluate_committed(
        assessment=assessment(evidence=("m1",), parent_should_know=True), assessment_id="a1"
    ) == "scheduled"
    assert await delivery.evaluate_committed(
        assessment=assessment(evidence=("m1",), parent_should_know=True), assessment_id="a2"
    ) == "duplicate_evidence"
    assert await delivery.evaluate_committed(
        assessment=assessment(evidence=("m1", "m2"), parent_should_know=True), assessment_id="a3"
    ) == "scheduled"
    assert len(scheduling.actions) == 2
    template = scheduling.actions[0].payload["template"]
    assert template["body_params"] == ["Shay", "Classmates repeatedly mock Shay.", "Class"]
    assert "url_suffix" not in template
    assert (await outcomes.get("a2")).reason == "already_notified"
    assert await delivery.evaluate_committed(
        assessment=assessment(evidence=("m1",), parent_should_know=True), assessment_id="a1"
    ) == "duplicate_evidence"
    await delivery.mark_evaluation_failed(assessment_id="a1", error="late failure")
    assert (await outcomes.get("a1")).status is ParentAttentionOutcomeStatus.FAILED
    await delivery.mark_evaluation_failed(assessment_id="missing", error="ignored")
    assert await delivery.validate_before_send({"guardian_user_id": "guardian", "child_user_id": "child"})
    assert not await delivery.validate_before_send({"guardian_user_id": "", "child_user_id": "child"})
    await delivery_repo.resolve(child_user_id="child", chat_id="class@g.us")
    resolved = await delivery_repo.get(child_user_id="child", chat_id="class@g.us")
    assert resolved is not None and resolved.resolved_at is not None


async def test_delivery_evaluation_failure_is_recorded() -> None:
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=NOW)
    outcomes = InMemoryParentAttentionDeliveryOutcomeRepository()
    delivery, _, _ = service(links=links, scheduling=FailingScheduling(), outcomes=outcomes)
    with pytest.raises(RuntimeError, match="delivery evaluation failed"):
        await delivery.evaluate_committed(
            assessment=assessment(evidence=("m1",), parent_should_know=True),
            assessment_id="failed-evaluation",
        )
    outcome = await outcomes.get("failed-evaluation")
    assert outcome is not None and outcome.reason == "pending_evaluation"
    await delivery.mark_evaluation_failed(
        assessment_id="failed-evaluation", error="delivery evaluation failed"
    )
    assert (await outcomes.get("failed-evaluation")).status is ParentAttentionOutcomeStatus.FAILED


async def test_reconcile_handles_pending_missing_and_suppressed_actions() -> None:
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=NOW)
    outcomes = InMemoryParentAttentionDeliveryOutcomeRepository()
    assessments = InMemoryGuardSimpleAssessmentRepository()
    actions = InMemoryScheduledActionRepository()
    scheduling = FakeScheduling()
    delivery = ParentAttentionDeliveryService(
        links=links,
        users=FakeUsers(),
        delivery=InMemoryParentAttentionDeliveryRepository(),
        outcomes=outcomes,
        assessments=assessments,
        actions=actions,
        scheduling=scheduling,
        enabled=True,
    )
    for assessment_id, action_id in (("no-action", None), ("missing-action", "missing")):
        await outcomes.save(
            ParentAttentionDeliveryOutcome(
                assessment_id=assessment_id,
                child_user_id="child",
                chat_id="class@g.us",
                status=ParentAttentionOutcomeStatus.SCHEDULED,
                reason="scheduled",
                scheduled_action_id=action_id,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    cancelled = ScheduledAction(
        id="cancelled",
        user_id="guardian",
        type=ScheduledActionType.SEND_BOT_MESSAGE,
        execute_at_utc=NOW,
        timezone="UTC",
        status=ScheduledActionStatus.CANCELLED,
        payload={},
        error="stale",
    )
    await actions.save(cancelled)
    await outcomes.save(
        ParentAttentionDeliveryOutcome(
            assessment_id="cancelled",
            child_user_id="child",
            chat_id="class@g.us",
            status=ParentAttentionOutcomeStatus.SCHEDULED,
            reason="scheduled",
            scheduled_action_id="cancelled",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await outcomes.save(
        ParentAttentionDeliveryOutcome(
            assessment_id="pending-evaluation",
            child_user_id="child",
            chat_id="class@g.us",
            status=ParentAttentionOutcomeStatus.NOT_SENT,
            reason="pending_evaluation",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await outcomes.save(
        ParentAttentionDeliveryOutcome(
            assessment_id="missing-assessment",
            child_user_id="child",
            chat_id="class@g.us",
            status=ParentAttentionOutcomeStatus.NOT_SENT,
            reason="pending_evaluation",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    result = await delivery.reconcile_pending(limit=20)
    assert result == 5
    assert (await outcomes.get("no-action")).status is ParentAttentionOutcomeStatus.INDETERMINATE
    assert (await outcomes.get("missing-action")).status is ParentAttentionOutcomeStatus.INDETERMINATE
    assert (await outcomes.get("cancelled")).status is ParentAttentionOutcomeStatus.NOT_SENT
    assert (await outcomes.get("missing-assessment")).status is ParentAttentionOutcomeStatus.FAILED


async def test_reconcile_marks_evaluation_exception_failed() -> None:
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=NOW)
    outcomes = InMemoryParentAttentionDeliveryOutcomeRepository()
    assessments = InMemoryGuardSimpleAssessmentRepository()
    delivery, _, _ = service(
        links=links,
        scheduling=FailingScheduling(),
        outcomes=outcomes,
        assessments=assessments,
    )
    saved_id = await assessments.save(
        assessment(evidence=("m1",), parent_should_know=True)
    )
    await outcomes.save(
        ParentAttentionDeliveryOutcome(
            assessment_id=saved_id,
            child_user_id="child",
            chat_id="class@g.us",
            status=ParentAttentionOutcomeStatus.NOT_SENT,
            reason="pending_evaluation",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    assert await delivery.reconcile_pending(limit=1) == 1
    assert (await outcomes.get(saved_id)).status is ParentAttentionOutcomeStatus.FAILED


async def test_reconcile_preserves_pending_and_unconfirmed_states() -> None:
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=NOW)
    outcomes = InMemoryParentAttentionDeliveryOutcomeRepository()
    assessments = InMemoryGuardSimpleAssessmentRepository()
    actions = InMemoryScheduledActionRepository()
    delivery = ParentAttentionDeliveryService(
        links=links,
        users=FakeUsers(),
        delivery=InMemoryParentAttentionDeliveryRepository(),
        outcomes=outcomes,
        assessments=assessments,
        actions=actions,
        scheduling=FailingScheduling(),
        enabled=True,
    )
    pending = assessment(evidence=("pending",), parent_should_know=True)
    await assessments.save(pending)
    for assessment_id, action_id, status, result, error in (
        ("pending", "pending-action", ScheduledActionStatus.PENDING, None, None),
        ("waiting", "waiting-action", ScheduledActionStatus.PENDING, None, None),
        ("no-provider", "no-provider-action", ScheduledActionStatus.SUCCEEDED, {}, None),
        ("unknown-no-error", "unknown-action", ScheduledActionStatus.INDETERMINATE, None, None),
    ):
        await outcomes.save(
            ParentAttentionDeliveryOutcome(
                assessment_id=assessment_id,
                child_user_id="child",
                chat_id="class@g.us",
                status=(
                    ParentAttentionOutcomeStatus.NOT_SENT
                    if assessment_id == "pending"
                    else ParentAttentionOutcomeStatus.SCHEDULED
                ),
                reason="pending_evaluation" if assessment_id == "pending" else "scheduled",
                scheduled_action_id=action_id,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await actions.save(
            ScheduledAction(
                id=action_id,
                user_id="guardian",
                type=ScheduledActionType.SEND_BOT_MESSAGE,
                execute_at_utc=NOW,
                timezone="UTC",
                status=status,
                payload={},
                result=result,
                error=error,
            )
        )
    assert await delivery.reconcile_pending(limit=10) == 3
    assert (await outcomes.get("pending")).status is ParentAttentionOutcomeStatus.FAILED
    assert (await outcomes.get("waiting")).status is ParentAttentionOutcomeStatus.SCHEDULED
    assert (await outcomes.get("no-provider")).status is ParentAttentionOutcomeStatus.INDETERMINATE
    assert (await outcomes.get("unknown-no-error")).reason == "provider_outcome_indeterminate"
    await outcomes.save(
        ParentAttentionDeliveryOutcome(
            assessment_id="unexpected-status",
            child_user_id="child",
            chat_id="class@g.us",
            status=ParentAttentionOutcomeStatus.SCHEDULED,
            reason="scheduled",
            scheduled_action_id="unexpected-action",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    unexpected = ScheduledAction(
        id="unexpected-action",
        user_id="guardian",
        type=ScheduledActionType.SEND_BOT_MESSAGE,
        execute_at_utc=NOW,
        timezone="UTC",
        status=object(),  # type: ignore[arg-type]
        payload={},
    )
    await actions.save(unexpected)
    assert await delivery.reconcile_pending(limit=10) == 1


async def test_reconcile_distinguishes_sent_failed_and_indeterminate() -> None:
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=NOW)
    outcomes = InMemoryParentAttentionDeliveryOutcomeRepository()
    actions = InMemoryScheduledActionRepository()
    scheduling = FakeScheduling()
    delivery = ParentAttentionDeliveryService(
        links=links,
        users=FakeUsers(),
        delivery=InMemoryParentAttentionDeliveryRepository(),
        outcomes=outcomes,
        assessments=InMemoryGuardSimpleAssessmentRepository(),
        actions=actions,
        scheduling=scheduling,
        enabled=True,
    )
    for assessment_id in ("sent", "failed", "unknown"):
        result = await delivery.evaluate_committed(
            assessment=assessment(evidence=(assessment_id,), parent_should_know=True),
            assessment_id=assessment_id,
        )
        assert result == "scheduled"
    for item, result, status in zip(
        scheduling.actions,
        (
            {"provider_message_id": "provider-1"},
            None,
            None,
        ),
        (
            ScheduledActionStatus.SUCCEEDED,
            ScheduledActionStatus.FAILED,
            ScheduledActionStatus.INDETERMINATE,
        ),
    ):
        if result is not None:
            await actions.save(replace(item, result=result, status=status, executed_at=NOW))
        else:
            await actions.save(replace(item, status=status, error="provider error", executed_at=NOW))
    assert await delivery.reconcile_pending(limit=10) == 3
    assert (await outcomes.get("sent")).status is ParentAttentionOutcomeStatus.SENT
    assert (await outcomes.get("failed")).status is ParentAttentionOutcomeStatus.FAILED
    assert (await outcomes.get("unknown")).status is ParentAttentionOutcomeStatus.INDETERMINATE
