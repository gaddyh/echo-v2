"""Tests for live Baseline B queue processing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
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


class FakeDelivery:
    def __init__(self, recovered: int = 0) -> None:
        self.assessments = []
        self.recovered = recovered

    async def evaluate_committed(self, *, assessment, assessment_id: str) -> str:
        self.assessments.append((assessment, assessment_id))
        return "scheduled"

    async def reconcile_pending(self, *, limit: int) -> int:
        return self.recovered


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
    outcome = await outcomes.get("guard-simple-1")
    assert outcome is not None and outcome.reason == "pending_evaluation"
    state = await states.get("child", "chat@c.us")
    assert state is not None
    assert state.last_parent_should_know is True
    assert state.last_analyzed_version == state.activity_version


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
