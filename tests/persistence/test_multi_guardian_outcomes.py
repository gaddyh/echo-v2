"""PR1 acceptance test: one assessment/result stores two independent guardian outcomes.

This proves the multi-guardian data foundation: a single analysis result can have
separate alert evaluations and delivery outcomes per guardian, keyed by
``(analysis_result_id, guardian_user_id)`` and ``(assessment_id, guardian_user_id)``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from echo_v2.domain.guard_alert import (
    GuardAlertEvaluation,
    GuardAlertEvaluationOutcome,
)
from echo_v2.domain.parent_attention import (
    ParentAttentionDeliveryOutcome,
    ParentAttentionOutcomeStatus,
)
from echo_v2.persistence.guard_alert_repositories import InMemoryGuardAlertRepository
from echo_v2.persistence.parent_attention_delivery import (
    InMemoryParentAttentionDeliveryOutcomeRepository,
)

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


async def test_two_guardians_get_independent_alert_evaluations_for_one_result() -> None:
    """A single analysis result can have two independent alert evaluations, one per guardian."""
    repo = InMemoryGuardAlertRepository()
    result_id = "result-1"

    eval_guardian_a = GuardAlertEvaluation(
        analysis_result_id=result_id,
        guardian_user_id="guardian-a",
        child_user_id="child",
        chat_id="chat@c.us",
        target_version=1,
        policy_version="2026-10-20.multi-guardian",
        outcome=GuardAlertEvaluationOutcome.SCHEDULED,
        evaluated_at=NOW,
        alert_id="alert-a",
    )
    eval_guardian_b = GuardAlertEvaluation(
        analysis_result_id=result_id,
        guardian_user_id="guardian-b",
        child_user_id="child",
        chat_id="chat@c.us",
        target_version=1,
        policy_version="2026-10-20.multi-guardian",
        outcome=GuardAlertEvaluationOutcome.NO_ACTION,
        evaluated_at=NOW,
    )

    assert await repo.save_evaluation(eval_guardian_a) is True
    assert await repo.save_evaluation(eval_guardian_b) is True

    loaded_a = await repo.get_evaluation(result_id, "guardian-a")
    loaded_b = await repo.get_evaluation(result_id, "guardian-b")
    assert loaded_a is not None and loaded_a.outcome is GuardAlertEvaluationOutcome.SCHEDULED
    assert loaded_b is not None and loaded_b.outcome is GuardAlertEvaluationOutcome.NO_ACTION
    assert loaded_a.alert_id == "alert-a"
    assert loaded_b.alert_id is None

    assert await repo.save_evaluation(eval_guardian_a) is False
    assert await repo.save_evaluation(eval_guardian_b) is False


async def test_two_guardians_get_independent_delivery_outcomes_for_one_assessment() -> None:
    """A single assessment can have two independent delivery outcomes, one per guardian."""
    repo = InMemoryParentAttentionDeliveryOutcomeRepository()
    assessment_id = "assessment-1"

    outcome_guardian_a = ParentAttentionDeliveryOutcome(
        assessment_id=assessment_id,
        guardian_user_id="guardian-a",
        child_user_id="child",
        chat_id="chat@c.us",
        status=ParentAttentionOutcomeStatus.SENT,
        reason="provider_confirmed",
        provider_message_id="provider-a",
        created_at=NOW,
        updated_at=NOW,
        sent_at=NOW,
    )
    outcome_guardian_b = ParentAttentionDeliveryOutcome(
        assessment_id=assessment_id,
        guardian_user_id="guardian-b",
        child_user_id="child",
        chat_id="chat@c.us",
        status=ParentAttentionOutcomeStatus.FAILED,
        reason="scheduled_action_failed",
        created_at=NOW,
        updated_at=NOW,
        failed_at=NOW,
    )

    await repo.save(outcome_guardian_a)
    await repo.save(outcome_guardian_b)

    loaded_a = await repo.get(assessment_id, guardian_user_id="guardian-a")
    loaded_b = await repo.get(assessment_id, guardian_user_id="guardian-b")
    assert loaded_a is not None and loaded_a.status is ParentAttentionOutcomeStatus.SENT
    assert loaded_b is not None and loaded_b.status is ParentAttentionOutcomeStatus.FAILED
    assert loaded_a.provider_message_id == "provider-a"
    assert loaded_b.provider_message_id is None

    listed = await repo.list_for_assessments([assessment_id])
    assert len(listed) == 2
    guardian_ids = {item.guardian_user_id for item in listed}
    assert guardian_ids == {"guardian-a", "guardian-b"}
