"""Database-free contract coverage for the new Postgres repository adapters."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from echo_v2.domain.guard import GuardAnalysisRecord
from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
from echo_v2.domain.parent_attention import ParentAttentionDeliveryState
from echo_v2.persistence.postgres_guard import (
    PostgresGuardAnalysisCommitRepository,
    PostgresGuardAnalysisRepository,
    PostgresGuardChatStateRepository,
    PostgresGuardianChildLinkRepository,
)
from echo_v2.persistence.postgres_guard_simple import (
    PostgresGuardSimpleAssessmentRepository,
)
from echo_v2.persistence.postgres_parent_attention_delivery import (
    PostgresParentAttentionDeliveryRepository,
)
from echo_v2.services.guard_taxonomy import GuardDecision

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, *, scalar=None, row=None, rows=None, rowcount=1) -> None:
        self._scalar = scalar
        self._row = row
        self._rows = rows
        self.rowcount = rowcount

    def scalar_one(self):
        return self._scalar if self._scalar is not None else self._row

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        return self

    def all(self):
        return self._rows or []


class FakeSession:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def execute(self, statement):
        return self.results.pop(0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


class FakeSessionFactory:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __call__(self):
        return self.session


def simple_row() -> SimpleNamespace:
    return SimpleNamespace(
        id="assessment-id",
        child_user_id="child",
        connection_id="connection",
        chat_id="chat@c.us",
        chat_type="private",
        chat_name="Chat",
        timezone="Asia/Jerusalem",
        window_start=NOW.replace(hour=0),
        window_end=NOW,
        parent_should_know=True,
        child_role="target",
        summary="summary",
        reason="reason",
        evidence_message_ids=["m1"],
        model="model",
        prompt_version="prompt",
        analyzer_version="analyzer",
        truncated=False,
        omitted_message_count=0,
        identity_verified=True,
        identity_reason=None,
        diagnostics={},
        created_at=NOW,
        first_detected_at=NOW,
        last_evidence_at=NOW,
        resolved_at=None,
        resolved_by=None,
    )


def assessment() -> SafetyAssessment:
    return SafetyAssessment(
        parent_should_know=True,
        child_role=ChildRole.TARGET,
        summary="summary",
        evidence_message_ids=("m1",),
        reason="reason",
        child_user_id="child",
        child_name="Shay",
        connection_id="connection",
        chat_id="chat@c.us",
        chat_type="private",
        chat_name="Chat",
        timezone="Asia/Jerusalem",
        window_start=NOW.replace(hour=0),
        window_end=NOW,
        created_at=NOW,
        first_detected_at=NOW,
        last_evidence_at=NOW,
    )


def legacy_record(record_id: str | None = None) -> GuardAnalysisRecord:
    return GuardAnalysisRecord(
        child_user_id="child", connection_id="connection", chat_id="chat@c.us",
        target_version=1, signals=(), categories=(), confidence=1.0,
        reason="reason", evidence_message_ids=(), decision=GuardDecision.NONE,
        model="model", prompt_version="prompt", analyzer_version="analyzer",
        taxonomy_version="taxonomy", id=record_id, created_at=NOW,
    )


async def test_postgres_guard_link_reads_active_rows() -> None:
    row = SimpleNamespace(
        id="link-id", guardian_user_id="guardian", child_user_id="child",
        status="active", child_consented_at=NOW, safety_enabled_at=NOW,
        created_at=NOW, updated_at=NOW,
    )
    session = FakeSession([FakeResult(row=row), FakeResult(rows=[row])])
    repository = PostgresGuardianChildLinkRepository(FakeSessionFactory(session))

    pair = await repository.get_active_for_pair(
        guardian_user_id="guardian", child_user_id="child"
    )
    listed = await repository.list_active_for_guardian("guardian")
    child_link = await PostgresGuardianChildLinkRepository(
        FakeSessionFactory(FakeSession([FakeResult(row=row)]))
    ).get_active_for_child("child")

    assert pair is not None and pair.child_user_id == "child"
    assert len(listed) == 1
    assert child_link is not None and child_link.guardian_user_id == "guardian"


async def test_legacy_guard_commit_handles_missing_and_stale_state() -> None:
    missing = PostgresGuardAnalysisCommitRepository(
        FakeSessionFactory(FakeSession([FakeResult(row=None)]))
    )
    assert await missing.commit_if_current(
        child_user_id="child", chat_id="chat@c.us", target_version=1,
        record=legacy_record(),
    ) == ("missing", None)
    stale = PostgresGuardAnalysisCommitRepository(
        FakeSessionFactory(FakeSession([FakeResult(row=SimpleNamespace(activity_version=2))]))
    )
    assert await stale.commit_if_current(
        child_user_id="child", chat_id="chat@c.us", target_version=1,
        record=legacy_record(),
    ) == ("stale", None)


async def test_legacy_guard_analysis_postgres_adapter_small_contract() -> None:
    session = FakeSession([
        FakeResult(scalar="legacy-id"),
        FakeResult(rows=[]),
    ])
    repository = PostgresGuardAnalysisRepository(FakeSessionFactory(session))
    saved = await repository.save(legacy_record(record_id="provided-id"))
    listed = await repository.list_needing_alert_evaluation(since=NOW)

    assert saved == "legacy-id"
    assert listed == []


async def test_guard_state_postgres_adapter_tracks_connection_and_parent_result() -> None:
    row = SimpleNamespace(
        child_user_id="child",
        chat_id="chat@c.us",
        activity_version=1,
        last_message_at=NOW,
        last_analyzed_version=0,
        last_decision="none",
        last_parent_should_know=None,
        connection_id="connection",
        last_analysis_at=None,
        pending_since=NOW,
        next_analysis_at=NOW,
        next_analysis_reason="fixed",
        chat_name="Chat",
        is_group=False,
        created_at=NOW,
        updated_at=NOW,
    )
    session = FakeSession([FakeResult(), FakeResult(row=row), FakeResult(rowcount=1)])
    repository = PostgresGuardChatStateRepository(FakeSessionFactory(session))

    state = await repository.upsert_on_message(
        child_user_id="child", chat_id="chat@c.us", observed_at=NOW,
        next_analysis_at=NOW, pending_since=NOW, connection_id="connection",
        next_analysis_reason="fixed", chat_name="Chat",
    )
    assert state.connection_id == "connection"
    assert await repository.mark_processed(
        "child", "chat@c.us", 1, decision=GuardDecision.NONE,
        analyzed_at=NOW, parent_should_know=True,
    )


async def test_guard_state_postgres_read_methods() -> None:
    row = SimpleNamespace(
        child_user_id="child", chat_id="chat@c.us", activity_version=1,
        last_message_at=NOW, last_analyzed_version=0, last_decision="none",
        last_parent_should_know=None, connection_id="connection",
        last_analysis_at=None, pending_since=NOW, next_analysis_at=NOW,
        next_analysis_reason="fixed", chat_name="Chat", is_group=False,
        created_at=NOW, updated_at=NOW,
    )
    session = FakeSession([
        FakeResult(row=row), FakeResult(rows=[row]), FakeResult(rows=[row])
    ])
    repository = PostgresGuardChatStateRepository(FakeSessionFactory(session))

    assert await repository.get("child", "chat@c.us") is not None
    assert len(await repository.list_due(NOW)) == 1
    assert len(await repository.list_for_children(["child"])) == 1

    empty = FakeSession([FakeResult(row=None), FakeResult(rows=[])])
    empty_repository = PostgresGuardChatStateRepository(FakeSessionFactory(empty))
    assert await empty_repository.get("missing", "chat@c.us") is None
    assert await empty_repository.list_due(NOW) == []
    assert await empty_repository.list_for_children([]) == []

    stale_session = FakeSession([FakeResult(rowcount=0)])
    stale_repository = PostgresGuardChatStateRepository(FakeSessionFactory(stale_session))
    assert not await stale_repository.mark_processed(
        "child", "chat@c.us", 1, decision=GuardDecision.NONE,
        analyzed_at=NOW, parent_should_know=False,
    )


async def test_parent_attention_postgres_adapter_contract() -> None:
    session = FakeSession(
        [
            FakeResult(),
            FakeResult(row=SimpleNamespace(
                child_user_id="child", chat_id="chat@c.us",
                last_delivered_assessment_id="assessment-id",
                last_delivered_evidence_ids=["m1"], last_delivered_at=NOW,
                last_action_id="action-id", resolved_at=None,
            )),
            FakeResult(rowcount=1),
        ]
    )
    repository = PostgresParentAttentionDeliveryRepository(FakeSessionFactory(session))
    state = ParentAttentionDeliveryState(
        child_user_id="child", chat_id="chat@c.us",
        last_delivered_assessment_id="assessment-id",
        last_delivered_evidence_ids=("m1",), last_delivered_at=NOW,
        last_action_id="action-id",
    )

    await repository.save(state)
    loaded = await repository.get(child_user_id="child", chat_id="chat@c.us")
    await repository.resolve(child_user_id="child", chat_id="chat@c.us", resolved_at=NOW)

    assert loaded is not None
    assert loaded.last_delivered_evidence_ids == ("m1",)
    assert session.commits == 2


async def test_simple_postgres_adapter_contract() -> None:
    row = simple_row()
    state_row = SimpleNamespace(activity_version=3)
    session = FakeSession(
        [
            FakeResult(scalar="assessment-id"),
            FakeResult(scalar="assessment-id"),
            FakeResult(rows=[row]),
            FakeResult(row=row),
            FakeResult(rowcount=1),
            FakeResult(row=state_row),
            FakeResult(scalar="assessment-id"),
            FakeResult(rowcount=1),
        ]
    )
    repository = PostgresGuardSimpleAssessmentRepository(FakeSessionFactory(session))

    saved_id = await repository.save(assessment())
    saved_existing_id = await repository.save(replace(assessment(), id="existing-id"))
    listed = await repository.list_for_chat(child_user_id="child", chat_id="chat@c.us")
    latest = await repository.latest_unresolved(child_user_id="child", chat_id="chat@c.us")
    resolved = await repository.resolve(assessment_id="assessment-id", resolved_by="guardian")
    committed = await repository.commit_if_current(
        child_user_id="child", chat_id="chat@c.us", target_version=3,
        assessment=replace(assessment(), id="assessment-id"),
    )

    assert saved_id == "assessment-id"
    assert saved_existing_id == "assessment-id"
    assert listed[0].child_role is ChildRole.TARGET
    assert latest is not None and latest.parent_should_know is True
    assert resolved
    assert committed == ("committed", "assessment-id")


async def test_simple_postgres_latest_unresolved_supports_before_boundary() -> None:
    session = FakeSession([FakeResult(row=None)])
    repository = PostgresGuardSimpleAssessmentRepository(FakeSessionFactory(session))

    result = await repository.latest_unresolved(
        child_user_id="child", chat_id="chat@c.us", before=NOW
    )

    assert result is None


async def test_simple_postgres_commit_rejects_missing_state() -> None:
    session = FakeSession([FakeResult(row=None)])
    repository = PostgresGuardSimpleAssessmentRepository(FakeSessionFactory(session))

    result = await repository.commit_if_current(
        child_user_id="child", chat_id="chat@c.us", target_version=3, assessment=assessment()
    )

    assert result == ("stale", None)


async def test_simple_postgres_commit_rejects_stale_version() -> None:
    session = FakeSession([FakeResult(row=SimpleNamespace(activity_version=2))])
    repository = PostgresGuardSimpleAssessmentRepository(FakeSessionFactory(session))

    result = await repository.commit_if_current(
        child_user_id="child", chat_id="chat@c.us", target_version=3, assessment=assessment()
    )

    assert result == ("stale", None)


async def test_simple_postgres_commit_rejects_lost_activity_update() -> None:
    session = FakeSession([
        FakeResult(row=SimpleNamespace(activity_version=3)),
        FakeResult(scalar="assessment-id"),
        FakeResult(rowcount=0),
    ])
    repository = PostgresGuardSimpleAssessmentRepository(FakeSessionFactory(session))

    result = await repository.commit_if_current(
        child_user_id="child", chat_id="chat@c.us", target_version=3, assessment=assessment()
    )

    assert result == ("stale", None)
