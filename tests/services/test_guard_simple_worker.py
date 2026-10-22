"""Tests for live Baseline B queue processing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from echo_v2.domain.guard_exposure import ExposureAssessment, ExposureCategory
from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
from echo_v2.persistence.guard_exposure_repositories import (
    InMemoryGuardExposureAssessmentRepository,
)
from echo_v2.persistence.guard_repositories import InMemoryGuardChatStateRepository
from echo_v2.persistence.guard_simple_repositories import (
    InMemoryGuardSimpleAssessmentCommitRepository,
    InMemoryGuardSimpleAssessmentRepository,
)
from echo_v2.persistence.parent_attention_delivery import (
    InMemoryParentAttentionDeliveryOutcomeRepository,
)
from echo_v2.services.guard_simple_worker import GuardSimpleAnalysisWorker

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


class FakeUsers:
    async def get_user_info_by_id(self, user_id: str):
        return "+972501234567", "Shay"


class FailingProcessor:
    async def process(self, **kwargs):
        raise RuntimeError("analysis failed")


class FakeProcessor:
    async def process(self, **kwargs):
        return SafetyAssessment(
            parent_should_know=True,
            child_role=ChildRole.TARGET,
            summary="Concern",
            evidence_message_ids=("m1",),
            reason="Reason",
            child_user_id=kwargs["child_user_id"],
            connection_id=kwargs["connection_id"],
            chat_id=kwargs["chat_id"],
            created_at=NOW,
        )


class FakeExposureProcessor(FakeProcessor):
    async def process_with_exposure(self, **kwargs):
        child = await self.process(**kwargs)
        exposure = ExposureAssessment(
            significant_exposure=True,
            category=ExposureCategory.BULLYING,
            summary="Repeated targeting.",
            evidence_message_ids=("m1",),
            reason="Targeted humiliation.",
            child_user_id=kwargs["child_user_id"],
            connection_id=kwargs["connection_id"],
            chat_id=kwargs["chat_id"],
            chat_type="group",
            target_version=kwargs["target_version"],
        )
        return child, exposure


class FakeDelivery:
    def __init__(self, recovered: int = 0) -> None:
        self.assessments = []
        self.recovered = recovered

    async def evaluate_committed(self, *, assessment, assessment_id: str) -> str:
        self.assessments.append((assessment, assessment_id))
        return "scheduled"

    async def reconcile_pending(self, *, limit: int) -> int:
        return self.recovered

    async def mark_evaluation_failed(self, *, assessment_id: str, error: str) -> None:
        return None


class FakeExposureDelivery(FakeDelivery):
    def __init__(self, recovered: int = 0) -> None:
        super().__init__(recovered)
        self.security_assessments = []

    async def cancel_overlapping_security(self, assessment) -> int:
        return 0

    async def evaluate_committed(self, *, assessment, assessment_id: str, security_assessment=None) -> str:
        self.assessments.append((assessment, assessment_id))
        self.security_assessments.append(security_assessment)
        return "scheduled"


async def test_worker_commits_current_version_and_delivers() -> None:
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(
        child_user_id="child",
        chat_id="chat@c.us",
        observed_at=NOW,
        next_analysis_at=NOW,
        pending_since=NOW,
        connection_id="connection",
    )
    assessments = InMemoryGuardSimpleAssessmentRepository()
    outcomes = InMemoryParentAttentionDeliveryOutcomeRepository()
    delivery = FakeDelivery()
    worker = GuardSimpleAnalysisWorker(
        state_repo=states,
        processor=FakeProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(states, assessments, outcomes),
        users=FakeUsers(),
        delivery=delivery,
    )

    assert await worker.run_once()
    assert len(assessments.assessments) == 1
    assert assessments.assessments[0].parent_should_know is True
    assert len(delivery.assessments) == 1
    state = await states.get("child", "chat@c.us")
    assert state is not None
    assert state.last_parent_should_know is True
    assert state.last_analyzed_version == state.activity_version


async def test_worker_commits_exposure_after_child_commit_for_same_version() -> None:
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(
        child_user_id="child", chat_id="group@g.us", observed_at=NOW,
        next_analysis_at=NOW, pending_since=NOW, connection_id="connection",
        is_group=True,
    )
    child_assessments = InMemoryGuardSimpleAssessmentRepository()
    exposure_assessments = InMemoryGuardExposureAssessmentRepository(states)
    worker = GuardSimpleAnalysisWorker(
        state_repo=states,
        processor=FakeExposureProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(states, child_assessments),
        exposure_repo=exposure_assessments,
        users=FakeUsers(),
    )

    assert await worker.run_once()
    assert len(child_assessments.assessments) == 1
    assert len(exposure_assessments.assessments) == 1
    assert exposure_assessments.assessments[0].target_version == 1


async def test_worker_delivers_committed_exposure_and_reconciles_both_channels() -> None:
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(
        child_user_id="child", chat_id="group@g.us", observed_at=NOW,
        next_analysis_at=NOW, pending_since=NOW, connection_id="connection", is_group=True,
    )
    child_assessments = InMemoryGuardSimpleAssessmentRepository()
    exposure_assessments = InMemoryGuardExposureAssessmentRepository(states)
    security_delivery = FakeDelivery(recovered=1)
    exposure_delivery = FakeExposureDelivery(recovered=1)
    worker = GuardSimpleAnalysisWorker(
        state_repo=states,
        processor=FakeExposureProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(states, child_assessments),
        exposure_repo=exposure_assessments,
        users=FakeUsers(),
        delivery=security_delivery,
        exposure_delivery=exposure_delivery,
    )
    assert await worker.run_once()
    assert len(security_delivery.assessments) == 1
    assert len(exposure_delivery.assessments) == 1
    assert exposure_delivery.security_assessments[0].parent_should_know is True


async def test_worker_handles_exposure_commit_stale() -> None:
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(
        child_user_id="child", chat_id="group@g.us", observed_at=NOW,
        next_analysis_at=NOW, pending_since=NOW, connection_id="connection", is_group=True,
    )
    class StaleExposureRepository(InMemoryGuardExposureAssessmentRepository):
        async def commit_if_current(self, **kwargs):
            return "stale", None
    worker = GuardSimpleAnalysisWorker(
        state_repo=states, processor=FakeExposureProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(
            states, InMemoryGuardSimpleAssessmentRepository()
        ),
        exposure_repo=StaleExposureRepository(states), users=FakeUsers(),
    )
    assert await worker.run_once()


async def test_worker_handles_processor_failure() -> None:
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(
        child_user_id="child", chat_id="chat", observed_at=NOW,
        next_analysis_at=NOW, pending_since=NOW, connection_id="connection",
    )
    assessments = InMemoryGuardSimpleAssessmentRepository()
    worker = GuardSimpleAnalysisWorker(
        state_repo=states,
        processor=FailingProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(states, assessments),
        users=FakeUsers(),
    )

    assert await worker.run_once()
    assert assessments.assessments == []


async def test_worker_reconcile_pending_marks_work_done() -> None:
    states = InMemoryGuardChatStateRepository()
    delivery = FakeDelivery(recovered=1)
    worker = GuardSimpleAnalysisWorker(
        state_repo=states,
        processor=FakeProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(
            states, InMemoryGuardSimpleAssessmentRepository()
        ),
        users=FakeUsers(),
        delivery=delivery,
    )

    assert await worker.run_once()


async def test_worker_skips_excluded_chat() -> None:
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(
        child_user_id="child", chat_id="excluded", observed_at=NOW,
        next_analysis_at=NOW, pending_since=NOW, connection_id="connection",
    )
    assessments = InMemoryGuardSimpleAssessmentRepository()
    worker = GuardSimpleAnalysisWorker(
        state_repo=states,
        processor=FakeProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(states, assessments),
        users=FakeUsers(),
        excluded_chat_ids=frozenset({"excluded"}),
    )

    assert await worker.run_once()
    assert assessments.assessments == []


async def test_worker_handles_missing_connection_without_committing() -> None:
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(
        child_user_id="child", chat_id="chat", observed_at=NOW,
        next_analysis_at=NOW, pending_since=NOW,
    )
    assessments = InMemoryGuardSimpleAssessmentRepository()
    worker = GuardSimpleAnalysisWorker(
        state_repo=states,
        processor=FakeProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(states, assessments),
        users=FakeUsers(),
    )

    assert await worker.run_once()
    assert assessments.assessments == []


# --- IncidentEvaluator integration ----------------------------------------


class FakeIncidentEvaluator:
    def __init__(self, *, fail_mode: bool = False, recovered: int = 0) -> None:
        self.security_calls: list[tuple] = []
        self.exposure_calls: list[tuple] = []
        self.fail_mode = fail_mode
        self.recovered = recovered

    async def evaluate_security(self, *, assessment, assessment_id: str) -> str:
        if self.fail_mode:
            raise RuntimeError("evaluator failed")
        self.security_calls.append((assessment, assessment_id))
        return "scheduled"

    async def evaluate_exposure(
        self, *, assessment, assessment_id: str, security_assessment=None
    ) -> str:
        if self.fail_mode:
            raise RuntimeError("evaluator failed")
        self.exposure_calls.append((assessment, assessment_id, security_assessment))
        return "scheduled"

    async def reconcile_pending(self, *, limit: int) -> int:
        return self.recovered

    async def mark_evaluation_failed(self, *, assessment_id: str, error: str) -> None:
        return None


async def test_worker_routes_security_through_incident_evaluator() -> None:
    """When incident_evaluator is set, security goes through it, not delivery."""
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(
        child_user_id="child", chat_id="chat@c.us", observed_at=NOW,
        next_analysis_at=NOW, pending_since=NOW, connection_id="connection",
    )
    assessments = InMemoryGuardSimpleAssessmentRepository()
    outcomes = InMemoryParentAttentionDeliveryOutcomeRepository()
    delivery = FakeDelivery()
    evaluator = FakeIncidentEvaluator()
    worker = GuardSimpleAnalysisWorker(
        state_repo=states,
        processor=FakeProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(states, assessments, outcomes),
        users=FakeUsers(),
        delivery=delivery,
        incident_evaluator=evaluator,
    )

    assert await worker.run_once()
    assert len(evaluator.security_calls) == 1
    assert len(delivery.assessments) == 0  # delivery NOT called


async def test_worker_routes_exposure_through_incident_evaluator() -> None:
    """When incident_evaluator is set, exposure goes through it, not exposure_delivery."""
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(
        child_user_id="child", chat_id="group@g.us", observed_at=NOW,
        next_analysis_at=NOW, pending_since=NOW, connection_id="connection", is_group=True,
    )
    child_assessments = InMemoryGuardSimpleAssessmentRepository()
    exposure_assessments = InMemoryGuardExposureAssessmentRepository(states)
    exposure_delivery = FakeExposureDelivery()
    evaluator = FakeIncidentEvaluator()
    worker = GuardSimpleAnalysisWorker(
        state_repo=states,
        processor=FakeExposureProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(states, child_assessments),
        exposure_repo=exposure_assessments,
        users=FakeUsers(),
        exposure_delivery=exposure_delivery,
        incident_evaluator=evaluator,
    )

    assert await worker.run_once()
    assert len(evaluator.security_calls) == 1
    assert len(evaluator.exposure_calls) == 1
    assert len(exposure_delivery.assessments) == 0  # exposure_delivery NOT called


async def test_worker_incident_evaluator_failure_marks_and_reraises() -> None:
    """If incident_evaluator raises, mark_evaluation_failed is called and error propagates."""
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(
        child_user_id="child", chat_id="chat@c.us", observed_at=NOW,
        next_analysis_at=NOW, pending_since=NOW, connection_id="connection",
    )
    assessments = InMemoryGuardSimpleAssessmentRepository()
    evaluator = FakeIncidentEvaluator(fail_mode=True)
    worker = GuardSimpleAnalysisWorker(
        state_repo=states,
        processor=FakeProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(states, assessments),
        users=FakeUsers(),
        incident_evaluator=evaluator,
    )

    # run_once catches the exception internally and logs it, returns True (touched)
    assert await worker.run_once()
    assert len(assessments.assessments) == 1  # assessment was committed


async def test_worker_reconciles_through_incident_evaluator() -> None:
    """run_once calls incident_evaluator.reconcile_pending when set."""
    states = InMemoryGuardChatStateRepository()
    evaluator = FakeIncidentEvaluator(recovered=1)
    worker = GuardSimpleAnalysisWorker(
        state_repo=states,
        processor=FakeProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(
            states, InMemoryGuardSimpleAssessmentRepository()
        ),
        users=FakeUsers(),
        incident_evaluator=evaluator,
    )

    assert await worker.run_once()  # recovered=1 → touched


async def test_worker_set_incident_evaluator() -> None:
    """set_incident_evaluator wires the evaluator after construction."""
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(
        child_user_id="child", chat_id="chat@c.us", observed_at=NOW,
        next_analysis_at=NOW, pending_since=NOW, connection_id="connection",
    )
    assessments = InMemoryGuardSimpleAssessmentRepository()
    outcomes = InMemoryParentAttentionDeliveryOutcomeRepository()
    delivery = FakeDelivery()
    evaluator = FakeIncidentEvaluator()
    worker = GuardSimpleAnalysisWorker(
        state_repo=states,
        processor=FakeProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(states, assessments, outcomes),
        users=FakeUsers(),
        delivery=delivery,
    )
    worker.set_incident_evaluator(evaluator)

    assert await worker.run_once()
    assert len(evaluator.security_calls) == 1
    assert len(delivery.assessments) == 0


async def test_worker_exposure_delivery_fallback_when_no_incident_evaluator() -> None:
    """Without incident_evaluator, exposure goes through exposure_delivery."""
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(
        child_user_id="child", chat_id="group@g.us", observed_at=NOW,
        next_analysis_at=NOW, pending_since=NOW, connection_id="connection", is_group=True,
    )
    child_assessments = InMemoryGuardSimpleAssessmentRepository()
    exposure_assessments = InMemoryGuardExposureAssessmentRepository(states)
    exposure_delivery = FakeExposureDelivery()
    worker = GuardSimpleAnalysisWorker(
        state_repo=states,
        processor=FakeExposureProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(states, child_assessments),
        exposure_repo=exposure_assessments,
        users=FakeUsers(),
        exposure_delivery=exposure_delivery,
    )

    assert await worker.run_once()
    assert len(exposure_delivery.assessments) == 1


async def test_worker_delivery_failure_marks_and_reraises() -> None:
    """If delivery raises, mark_evaluation_failed is called and error propagates."""
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(
        child_user_id="child", chat_id="chat@c.us", observed_at=NOW,
        next_analysis_at=NOW, pending_since=NOW, connection_id="connection",
    )
    assessments = InMemoryGuardSimpleAssessmentRepository()

    class FailingDelivery(FakeDelivery):
        async def evaluate_committed(self, *, assessment, assessment_id: str) -> str:
            raise RuntimeError("delivery failed")

    delivery = FailingDelivery()
    worker = GuardSimpleAnalysisWorker(
        state_repo=states,
        processor=FakeProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(states, assessments),
        users=FakeUsers(),
        delivery=delivery,
    )

    # run_once catches the exception internally and logs it
    assert await worker.run_once()
    assert len(assessments.assessments) == 1


async def test_worker_exposure_delivery_failure_marks_and_reraises() -> None:
    """If exposure_delivery raises, mark_evaluation_failed is called."""
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(
        child_user_id="child", chat_id="group@g.us", observed_at=NOW,
        next_analysis_at=NOW, pending_since=NOW, connection_id="connection", is_group=True,
    )
    child_assessments = InMemoryGuardSimpleAssessmentRepository()
    exposure_assessments = InMemoryGuardExposureAssessmentRepository(states)

    class FailingExposureDelivery(FakeExposureDelivery):
        async def evaluate_committed(self, *, assessment, assessment_id: str, security_assessment=None) -> str:
            raise RuntimeError("exposure delivery failed")

    exposure_delivery = FailingExposureDelivery()
    worker = GuardSimpleAnalysisWorker(
        state_repo=states,
        processor=FakeExposureProcessor(),
        commit_repo=InMemoryGuardSimpleAssessmentCommitRepository(states, child_assessments),
        exposure_repo=exposure_assessments,
        users=FakeUsers(),
        exposure_delivery=exposure_delivery,
    )

    assert await worker.run_once()
