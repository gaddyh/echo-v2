"""PR2 integration test: two guardians, partial failure, independent retry.

Proves the multi-guardian fan-out architecture:
- One assessment is evaluated once.
- Two guardians each get independent delivery outcomes.
- One guardian's scheduling fails; the other still succeeds.
- The failed guardian can be retried independently.
- Telemetry shows per-guardian delivery details.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
from echo_v2.domain.parent_attention import (
    ParentAttentionDeliveryOutcome,
    ParentAttentionOutcomeStatus,
)
from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
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
NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


class FakeUsers:
    async def get_phone_by_id(self, user_id: str) -> str | None:
        return f"+97250123456{user_id[-1]}"


class SelectiveFailingScheduling:
    """Fails scheduling for guardian-b but succeeds for guardian-a."""

    def __init__(self) -> None:
        self.actions: list[ScheduledAction] = []

    async def create_once(self, **kwargs):
        user_id = kwargs["user_id"]
        if user_id == "guardian-b":
            raise RuntimeError("guardian-b scheduling failed")
        action = ScheduledAction(
            id=f"action-{len(self.actions) + 1}",
            user_id=user_id,
            type=kwargs["type"],
            execute_at_utc=kwargs["execute_at_utc"],
            timezone=kwargs["timezone_name"],
            status=ScheduledActionStatus.PENDING,
            payload=kwargs["payload"],
        )
        self.actions.append(action)
        return action, True


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


async def test_two_guardians_independent_outcomes_partial_failure() -> None:
    """Guardian-a schedules successfully; guardian-b fails; both have independent outcomes."""
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian-a", child_user_id="child", now=NOW)
    await links.upsert_active(guardian_user_id="guardian-b", child_user_id="child", now=NOW)

    outcomes = InMemoryParentAttentionDeliveryOutcomeRepository()
    scheduling = SelectiveFailingScheduling()
    delivery = ParentAttentionDeliveryService(
        links=links,
        users=FakeUsers(),
        delivery=InMemoryParentAttentionDeliveryRepository(),
        outcomes=outcomes,
        assessments=InMemoryGuardSimpleAssessmentRepository(),
        actions=InMemoryScheduledActionRepository(),
        scheduling=scheduling,
        enabled=True,
    )

    result = await delivery.evaluate_committed(
        assessment=assessment(evidence=("m1",), parent_should_know=True),
        assessment_id="a1",
    )
    assert result == "scheduled"

    # Guardian-a: scheduled successfully.
    outcome_a = await outcomes.get("a1", guardian_user_id="guardian-a")
    assert outcome_a is not None
    assert outcome_a.status is ParentAttentionOutcomeStatus.SCHEDULED
    assert outcome_a.scheduled_action_id is not None

    # Guardian-b: failed due to scheduling error.
    outcome_b = await outcomes.get("a1", guardian_user_id="guardian-b")
    assert outcome_b is not None
    assert outcome_b.status is ParentAttentionOutcomeStatus.FAILED
    assert outcome_b.reason == "delivery_evaluation_error"

    # Only one action was created (for guardian-a).
    assert len(scheduling.actions) == 1
    assert scheduling.actions[0].user_id == "guardian-a"


async def test_retry_failed_guardian_independently() -> None:
    """After partial failure, retrying only re-evaluates the failed guardian."""
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian-a", child_user_id="child", now=NOW)
    await links.upsert_active(guardian_user_id="guardian-b", child_user_id="child", now=NOW)

    outcomes = InMemoryParentAttentionDeliveryOutcomeRepository()
    failing_scheduling = SelectiveFailingScheduling()
    delivery = ParentAttentionDeliveryService(
        links=links,
        users=FakeUsers(),
        delivery=InMemoryParentAttentionDeliveryRepository(),
        outcomes=outcomes,
        assessments=InMemoryGuardSimpleAssessmentRepository(),
        actions=InMemoryScheduledActionRepository(),
        scheduling=failing_scheduling,
        enabled=True,
    )

    # First evaluation: guardian-a succeeds, guardian-b fails.
    assert await delivery.evaluate_committed(
        assessment=assessment(evidence=("m1",), parent_should_know=True),
        assessment_id="a1",
    ) == "scheduled"
    assert len(failing_scheduling.actions) == 1

    # Retry with a working scheduler.
    working_scheduling = FakeScheduling()
    delivery._scheduling = working_scheduling
    assert await delivery.evaluate_committed(
        assessment=assessment(evidence=("m1",), parent_should_know=True),
        assessment_id="a1",
    ) == "scheduled"

    # Guardian-a was already scheduled (not re-scheduled).
    # Guardian-b was failed and is now scheduled.
    outcome_a = await outcomes.get("a1", guardian_user_id="guardian-a")
    assert outcome_a is not None
    assert outcome_a.status is ParentAttentionOutcomeStatus.SCHEDULED

    outcome_b = await outcomes.get("a1", guardian_user_id="guardian-b")
    assert outcome_b is not None
    assert outcome_b.status is ParentAttentionOutcomeStatus.SCHEDULED

    # Only one new action was created (for guardian-b retry).
    assert len(working_scheduling.actions) == 1
    assert working_scheduling.actions[0].user_id == "guardian-b"


async def test_two_guardians_both_succeed() -> None:
    """Both guardians schedule independently with separate actions and request IDs."""
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian-a", child_user_id="child", now=NOW)
    await links.upsert_active(guardian_user_id="guardian-b", child_user_id="child", now=NOW)

    outcomes = InMemoryParentAttentionDeliveryOutcomeRepository()
    scheduling = FakeScheduling()
    delivery = ParentAttentionDeliveryService(
        links=links,
        users=FakeUsers(),
        delivery=InMemoryParentAttentionDeliveryRepository(),
        outcomes=outcomes,
        assessments=InMemoryGuardSimpleAssessmentRepository(),
        actions=InMemoryScheduledActionRepository(),
        scheduling=scheduling,
        enabled=True,
    )

    assert await delivery.evaluate_committed(
        assessment=assessment(evidence=("m1",), parent_should_know=True),
        assessment_id="a1",
    ) == "scheduled"

    # Both guardians have independent SCHEDULED outcomes.
    outcome_a = await outcomes.get("a1", guardian_user_id="guardian-a")
    outcome_b = await outcomes.get("a1", guardian_user_id="guardian-b")
    assert outcome_a is not None and outcome_a.status is ParentAttentionOutcomeStatus.SCHEDULED
    assert outcome_b is not None and outcome_b.status is ParentAttentionOutcomeStatus.SCHEDULED
    assert outcome_a.scheduled_action_id != outcome_b.scheduled_action_id

    # Two separate actions were created.
    assert len(scheduling.actions) == 2
    action_user_ids = {a.user_id for a in scheduling.actions}
    assert action_user_ids == {"guardian-a", "guardian-b"}

    # Request IDs are guardian-specific.
    request_ids = {a.payload["idempotency_key"] for a in scheduling.actions}
    assert len(request_ids) == 2
    assert all("guardian-a" in rid or "guardian-b" in rid for rid in request_ids)


async def test_telemetry_shows_per_guardian_deliveries() -> None:
    """Telemetry activity view includes per-guardian delivery details."""
    from echo_v2.services.guard_simple_telemetry_service import _activity_view

    assessment_obj = assessment(evidence=("m1",), parent_should_know=True)
    assessment_obj = replace(assessment_obj, id="a1")

    outcome_a = ParentAttentionDeliveryOutcome(
        assessment_id="a1",
        guardian_user_id="guardian-a",
        child_user_id="child",
        chat_id="class@g.us",
        status=ParentAttentionOutcomeStatus.SENT,
        reason="provider_confirmed",
        provider_message_id="msg-a",
        created_at=NOW,
        updated_at=NOW,
        sent_at=NOW,
    )
    outcome_b = ParentAttentionDeliveryOutcome(
        assessment_id="a1",
        guardian_user_id="guardian-b",
        child_user_id="child",
        chat_id="class@g.us",
        status=ParentAttentionOutcomeStatus.FAILED,
        reason="delivery_evaluation_error",
        created_at=NOW,
        updated_at=NOW,
        failed_at=NOW,
    )

    view = _activity_view(assessment_obj, [outcome_a, outcome_b], None, "Shay")
    assert "deliveries" in view
    assert len(view["deliveries"]) == 2
    statuses = {d["status"] for d in view["deliveries"]}
    assert statuses == {"sent", "failed"}
    # Aggregate delivery picks the most informative (sent).
    assert view["delivery"]["status"] == "sent"


async def test_exposure_disabled_with_no_links_creates_placeholder() -> None:
    """Exposure service disabled with no links creates a placeholder outcome."""
    from echo_v2.domain.guard_exposure import ExposureAssessment, ExposureCategory
    from echo_v2.persistence.guard_exposure_delivery import (
        InMemoryExposureDeliveryRepository,
    )
    from echo_v2.persistence.guard_simple_repositories import (
        InMemoryGuardSimpleAssessmentRepository,
    )
    from echo_v2.persistence.scheduled_actions import InMemoryScheduledActionRepository
    from echo_v2.services.guard_exposure_delivery_service import ExposureDeliveryService

    links = InMemoryGuardianChildLinkRepository()
    delivery_repo = InMemoryExposureDeliveryRepository()
    service = ExposureDeliveryService(
        links=links,
        users=FakeUsers(),
        security_assessments=InMemoryGuardSimpleAssessmentRepository(),
        delivery=delivery_repo,
        actions=InMemoryScheduledActionRepository(),
        scheduling=FakeScheduling(),
        enabled=False,
    )
    exp = ExposureAssessment(
        id="e1",
        significant_exposure=True,
        category=ExposureCategory.BULLYING,
        summary="test",
        evidence_message_ids=("m1",),
        reason="test",
        input_message_ids=("m1",),
        child_user_id="child",
        connection_id="conn",
        chat_id="class@g.us",
        chat_type="group",
        chat_name="Class",
        timezone="UTC",
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        created_at=NOW,
    )
    result = await service.evaluate_committed(assessment=exp, assessment_id="e1")
    assert result == "disabled"
    outcome = await delivery_repo.get_outcome("e1", guardian_user_id="")
    assert outcome is not None and outcome.reason == "disabled"


async def test_exposure_partial_failure_isolates_per_guardian() -> None:
    """Exposure service isolates per-guardian scheduling failures."""
    from echo_v2.domain.guard_exposure import ExposureAssessment, ExposureCategory
    from echo_v2.persistence.guard_exposure_delivery import (
        InMemoryExposureDeliveryRepository,
    )
    from echo_v2.persistence.guard_simple_repositories import (
        InMemoryGuardSimpleAssessmentRepository,
    )
    from echo_v2.persistence.scheduled_actions import InMemoryScheduledActionRepository
    from echo_v2.services.guard_exposure_delivery_service import ExposureDeliveryService

    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian-a", child_user_id="child", now=NOW)
    await links.upsert_active(guardian_user_id="guardian-b", child_user_id="child", now=NOW)

    class FailingForB:
        def __init__(self):
            self.actions: list[ScheduledAction] = []

        async def create_once(self, **kwargs):
            if kwargs["user_id"] == "guardian-b":
                raise RuntimeError("guardian-b failed")
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

    delivery_repo = InMemoryExposureDeliveryRepository()
    service = ExposureDeliveryService(
        links=links,
        users=FakeUsers(),
        security_assessments=InMemoryGuardSimpleAssessmentRepository(),
        delivery=delivery_repo,
        actions=InMemoryScheduledActionRepository(),
        scheduling=FailingForB(),
        enabled=True,
    )
    exp = ExposureAssessment(
        id="e1",
        significant_exposure=True,
        category=ExposureCategory.BULLYING,
        summary="test",
        evidence_message_ids=("m1",),
        reason="test",
        input_message_ids=("m1",),
        child_user_id="child",
        connection_id="conn",
        chat_id="class@g.us",
        chat_type="group",
        chat_name="Class",
        timezone="UTC",
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        created_at=NOW,
    )
    result = await service.evaluate_committed(assessment=exp, assessment_id="e1")
    assert result == "scheduled"
    outcome_a = await delivery_repo.get_outcome("e1", guardian_user_id="guardian-a")
    outcome_b = await delivery_repo.get_outcome("e1", guardian_user_id="guardian-b")
    assert outcome_a is not None and outcome_a.status is ParentAttentionOutcomeStatus.SCHEDULED
    assert outcome_b is not None and outcome_b.status is ParentAttentionOutcomeStatus.FAILED
    assert outcome_b.reason == "delivery_evaluation_error"
