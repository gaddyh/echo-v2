"""Focused tests for the Guard shadow runtime seams."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.chat import Message
from echo_v2.domain.guard import GuardAnalysisRecord, GuardChatState
from echo_v2.persistence.chat_repositories import (
    InMemoryChatStateRepository,
    InMemoryIngestionRepository,
    InMemoryMessageRepository,
)
from echo_v2.persistence.guard_repositories import (
    InMemoryGuardAnalysisCommitRepository,
    InMemoryGuardAnalysisRepository,
    InMemoryGuardChatStateRepository,
    InMemoryGuardianChildLinkRepository,
)
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    MessageDirection,
    MessageKind,
    ProviderMessageEvent,
)
from echo_v2.services.chat_ingestion import ChatIngestionService
from echo_v2.services.guard_conversation import GuardConversationBuilder
from echo_v2.services.guard_schedule_policy import (
    GuardConversationState,
    GuardSchedulePolicy,
    GuardSchedulingContext,
)
from echo_v2.services.guard_taxonomy import GuardDecision

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


def _event(chat_id: str, message_id: str, at: datetime) -> ProviderMessageEvent:
    return ProviderMessageEvent(
        event_id=f"event:{message_id}",
        connection=ConnectionRef(provider="baileys", provider_connection_id="conn"),
        chat_id=chat_id,
        provider_message_id=message_id,
        direction=MessageDirection.INBOUND,
        source=None,
        timestamp=at,
        kind=MessageKind.TEXT,
        text="hello",
        sender_id="sender-a",
        sender_name="Noa",
        chat_name="Group",
        is_group=chat_id.endswith("@g.us"),
    )


async def test_guard_schedule_uses_decision_dependent_intervals() -> None:
    policy = GuardSchedulePolicy()
    for decision, seconds in (
        (GuardDecision.NONE, 120),
        (GuardDecision.WATCH, 60),
        (GuardDecision.CONCERNING, 30),
        (GuardDecision.URGENT, 0),
    ):
        schedule = policy.on_message(
            GuardSchedulingContext(
                state=GuardConversationState(decision=decision),
                is_group=False,
                pending_since=None,
                last_analysis_at=None,
                next_analysis_at=None,
                new_message_at=BASE,
            )
        )
        assert schedule.analyze_at == BASE + timedelta(seconds=seconds)
    earlier = policy.on_message(
        GuardSchedulingContext(
            state=GuardConversationState(decision=GuardDecision.NONE), is_group=False,
            pending_since=BASE, last_analysis_at=None,
            next_analysis_at=BASE + timedelta(seconds=30), new_message_at=BASE,
        )
    )
    assert earlier.reason == "existing_deadline_or_decision_deadline"
    assert earlier.analyze_at == BASE + timedelta(seconds=30)
    assert GuardSchedulePolicy.now().tzinfo is not None
    assert policy.after_analysis(
        state=GuardChatState(child_user_id="child", chat_id="chat", activity_version=1,
                             last_message_at=BASE)
    ) is None


async def test_guard_queue_ownership_skips_wfm_and_allows_groups() -> None:
    messages = InMemoryMessageRepository()
    wfm_state = InMemoryChatStateRepository()
    guard_state = InMemoryGuardChatStateRepository()
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=BASE)
    service = ChatIngestionService(
        InMemoryIngestionRepository(messages, wfm_state, guard_state),
        child_link_repo=links,
        guard_chat_state_repo=guard_state,
        guard_shadow_enabled=True,
    )

    assert await service.ingest_message(
        _event("group@g.us", "one", BASE), user_id="child", connection_id="conn-uuid"
    )
    assert await wfm_state.get("child", "group@g.us") is None
    state = await guard_state.get("child", "group@g.us")
    assert state is not None
    assert state.next_analysis_at == BASE + timedelta(seconds=120)


async def test_guard_conversation_roles_are_stable_and_identity_free() -> None:
    messages = InMemoryMessageRepository()
    for sender_id, at in (("z", BASE), ("a", BASE + timedelta(seconds=1))):
        await messages.save(
            Message(
                id=str(uuid.uuid4()), user_id="child", connection_id="conn-uuid",
                chat_id="group@g.us", provider_message_id=str(uuid.uuid4()),
                direction=MessageDirection.INBOUND, sender_id=sender_id,
                sender_name="same display name", timestamp=at, text="hi",
            )
        )
    conversation = await GuardConversationBuilder(messages).build(
        child_user_id="child", chat_id="group@g.us"
    )
    assert [message.sender for message in conversation.input.messages] == ["other_2", "other"]
    assert all("same display name" not in message.text for message in conversation.input.messages)
    assert {item.role for item in conversation.participant_diagnostics} == {"other", "other_2"}


async def test_guard_commit_discards_stale_result() -> None:
    state = InMemoryGuardChatStateRepository()
    analyses = InMemoryGuardAnalysisRepository()
    commit = InMemoryGuardAnalysisCommitRepository(state, analyses)
    await state.upsert_on_message(
        child_user_id="child", chat_id="chat", observed_at=BASE,
        next_analysis_at=BASE, pending_since=BASE, chat_name=None, is_group=False,
    )
    await state.upsert_on_message(
        child_user_id="child", chat_id="chat", observed_at=BASE + timedelta(seconds=1),
        next_analysis_at=BASE, pending_since=BASE, chat_name=None, is_group=False,
    )
    record = GuardAnalysisRecord(
        child_user_id="child", connection_id="conn", chat_id="chat", target_version=1,
        signals=(), categories=(), confidence=1, reason="none", evidence_message_ids=(),
        decision=GuardDecision.NONE, model="test", prompt_version="test",
        analyzer_version="test", taxonomy_version="test",
    )
    assert await commit.commit_if_current(
        child_user_id="child", chat_id="chat", target_version=1, record=record
    ) == ("stale", None)
    assert analyses.records == []


async def test_guard_processor_replays_prior_observations_and_updates_decision() -> None:
    from echo_v2.services.guard_analysis import GuardAnalysisProcessor
    from echo_v2.services.guard_analyzer import GuardAnalysis

    messages = InMemoryMessageRepository()
    await messages.save(
        Message(
            id="m1", user_id="child", connection_id="conn", chat_id="chat@c.us",
            provider_message_id="m1", direction=MessageDirection.INBOUND,
            sender_id="sender", timestamp=BASE, text="meet me",
        )
    )
    analyses = InMemoryGuardAnalysisRepository()
    await analyses.save(
        GuardAnalysisRecord(
            child_user_id="child", connection_id="conn", chat_id="chat@c.us",
            target_version=1, signals=("meeting_request",), categories=(),
            confidence=0.8, reason="prior", evidence_message_ids=("m1",),
            decision=GuardDecision.WATCH, model="test", prompt_version="p",
            analyzer_version="a", taxonomy_version="t", created_at=BASE,
        )
    )

    class FakeAnalyzer:
        async def analyze(self, conversation):
            return GuardAnalysis(
                signals=("secrecy_request",), categories=("suspicious_contact",),
                evidence_message_ids=("m1",), confidence=0.9, reason="current",
                model="test", prompt_version="p", analyzer_version="a",
                taxonomy_version="t",
            )

    prepared = await GuardAnalysisProcessor(
        message_repo=messages, analyzer=FakeAnalyzer(), analysis_repo=analyses,
        signal_window_hours=24,
    ).process("child", "chat@c.us", 2)
    assert prepared.record.decision == GuardDecision.URGENT
    assert prepared.record.diagnostics["cumulative_signals"] == [
        "meeting_request", "secrecy_request"
    ]


async def test_guard_worker_commits_due_state() -> None:
    from echo_v2.services.guard_analysis import GuardAnalysisProcessor
    from echo_v2.services.guard_analysis_worker import GuardAnalysisWorker
    from echo_v2.services.guard_analyzer import GuardAnalysis

    messages = InMemoryMessageRepository()
    await messages.save(
        Message(
            id="m1", user_id="child", connection_id="conn", chat_id="chat@c.us",
            provider_message_id="m1", direction=MessageDirection.INBOUND,
            sender_id="sender", timestamp=BASE, text="hello",
        )
    )
    state = InMemoryGuardChatStateRepository()
    await state.upsert_on_message(
        child_user_id="child", chat_id="chat@c.us", observed_at=BASE,
        next_analysis_at=BASE - timedelta(days=1), pending_since=BASE, chat_name=None, is_group=False,
    )
    analyses = InMemoryGuardAnalysisRepository()

    class FakeAnalyzer:
        async def analyze(self, conversation):
            return GuardAnalysis(
                signals=(), categories=(), evidence_message_ids=(), confidence=1,
                reason="none", model="test", prompt_version="p", analyzer_version="a",
                taxonomy_version="t",
            )

    processor = GuardAnalysisProcessor(
        message_repo=messages, analyzer=FakeAnalyzer(), analysis_repo=analyses,
    )
    worker = GuardAnalysisWorker(
        state_repo=state, processor=processor,
        commit_repo=InMemoryGuardAnalysisCommitRepository(state, analyses),
    )
    assert await worker.run_once()
    current = await state.get("child", "chat@c.us")
    assert current is not None
    assert current.last_analyzed_version == 1
    assert current.next_analysis_at is None


async def test_guard_worker_returns_false_without_due_state() -> None:
    from echo_v2.services.guard_analysis_worker import GuardAnalysisWorker

    class UnusedProcessor:
        async def process(self, child_user_id, chat_id, target_version):
            raise AssertionError("not called")

    worker = GuardAnalysisWorker(
        state_repo=InMemoryGuardChatStateRepository(),
        processor=UnusedProcessor(),
        commit_repo=InMemoryGuardAnalysisCommitRepository(
            InMemoryGuardChatStateRepository(), InMemoryGuardAnalysisRepository()
        ),
    )
    assert await worker.run_once() is False


async def test_guard_in_memory_repositories_cover_missing_and_duplicate_paths() -> None:
    links = InMemoryGuardianChildLinkRepository()
    assert await links.get_active_for_child("missing") is None
    first = await links.upsert_active(
        guardian_user_id="guardian", child_user_id="child", now=BASE
    )
    second = await links.upsert_active(
        guardian_user_id="guardian", child_user_id="child", now=BASE + timedelta(seconds=1)
    )
    assert first.id == second.id

    state = InMemoryGuardChatStateRepository()
    analyses = InMemoryGuardAnalysisRepository()
    commit = InMemoryGuardAnalysisCommitRepository(state, analyses)
    record = GuardAnalysisRecord(
        child_user_id="child", connection_id="conn", chat_id="chat", target_version=1,
        signals=(), categories=(), confidence=1, reason="none", evidence_message_ids=(),
        decision=GuardDecision.NONE, model="m", prompt_version="p",
        analyzer_version="a", taxonomy_version="t",
    )
    assert await commit.commit_if_current(
        child_user_id="child", chat_id="chat", target_version=1, record=record
    ) == ("missing", None)
    await state.upsert_on_message(
        child_user_id="child", chat_id="chat", observed_at=BASE,
        next_analysis_at=BASE, pending_since=BASE, chat_name=None, is_group=False,
    )
    stale, _ = await commit.commit_if_current(
        child_user_id="child", chat_id="chat", target_version=2, record=record
    )
    assert stale == "stale"
    assert await analyses.save(record)
    with pytest.raises(ValueError, match="duplicate"):
        await analyses.save(record)
    duplicate_status, _ = await commit.commit_if_current(
        child_user_id="child", chat_id="chat", target_version=1, record=record
    )
    assert duplicate_status == "committed"
    assert not await state.mark_processed(
        "child", "chat", 99, decision=GuardDecision.NONE, analyzed_at=BASE
    )
    await state.upsert_on_message(
        child_user_id="child", chat_id="chat", observed_at=BASE - timedelta(seconds=1),
        next_analysis_at=BASE, pending_since=BASE, chat_name=None, is_group=False,
    )
    assert await links.get_active_for_child("other") is None

    class CommitRaceState(InMemoryGuardChatStateRepository):
        async def mark_processed(self, *args, **kwargs):
            return False

    race_state = CommitRaceState()
    await race_state.upsert_on_message(
        child_user_id="child", chat_id="race", observed_at=BASE,
        next_analysis_at=BASE, pending_since=BASE, chat_name=None, is_group=False,
    )
    race_commit = InMemoryGuardAnalysisCommitRepository(
        race_state, InMemoryGuardAnalysisRepository()
    )
    assert (await race_commit.commit_if_current(
        child_user_id="child", chat_id="race", target_version=1, record=record
    ))[0] == "stale"


async def test_guard_worker_drains_excluded_and_survives_processor_error() -> None:
    from echo_v2.services.guard_analysis_worker import GuardAnalysisWorker

    state = InMemoryGuardChatStateRepository()
    await state.upsert_on_message(
        child_user_id="child", chat_id="excluded", observed_at=BASE,
        next_analysis_at=BASE - timedelta(days=1), pending_since=BASE,
        chat_name=None, is_group=False,
    )

    class FailingProcessor:
        async def process(self, child_user_id, chat_id, target_version):
            raise RuntimeError("expected test failure")

    worker = GuardAnalysisWorker(
        state_repo=state, processor=FailingProcessor(),
        commit_repo=InMemoryGuardAnalysisCommitRepository(
            state, InMemoryGuardAnalysisRepository()
        ), excluded_chat_ids=frozenset({"excluded"}),
    )
    assert await worker.run_once()
    state = InMemoryGuardChatStateRepository()
    await state.upsert_on_message(
        child_user_id="child", chat_id="failing", observed_at=BASE,
        next_analysis_at=BASE - timedelta(days=1), pending_since=BASE,
        chat_name=None, is_group=False,
    )
    worker = GuardAnalysisWorker(
        state_repo=state, processor=FailingProcessor(),
        commit_repo=InMemoryGuardAnalysisCommitRepository(
            state, InMemoryGuardAnalysisRepository()
        ),
    )
    assert await worker.run_once()


async def test_guard_conversation_labels_unknown_participants() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(
        Message(
            id="unknown", user_id="child", connection_id="conn", chat_id="chat@g.us",
            provider_message_id="unknown", direction=MessageDirection.INBOUND,
            sender_id=None, timestamp=BASE, text="hello",
        )
    )
    conversation = await GuardConversationBuilder(messages).build(
        child_user_id="child", chat_id="chat@g.us"
    )
    assert conversation.input.messages[0].sender == "unknown_participant_1"


async def test_guard_conversation_rejects_inconsistent_connections() -> None:
    messages = InMemoryMessageRepository()
    for connection_id, message_id in (("conn-a", "a"), ("conn-b", "b")):
        await messages.save(
            Message(
                id=message_id, user_id="child", connection_id=connection_id,
                chat_id="chat@c.us", provider_message_id=message_id,
                direction=MessageDirection.INBOUND, sender_id=None,
                timestamp=BASE, text="hello",
            )
        )
    with pytest.raises(ValueError, match="inconsistent connections"):
        await GuardConversationBuilder(messages).build(
            child_user_id="child", chat_id="chat@c.us"
        )
