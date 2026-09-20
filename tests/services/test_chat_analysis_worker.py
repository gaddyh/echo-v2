"""ChatAnalysisWorker tests (in-memory repos, no Docker needed)."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from echo_v2.domain.chat import Message
from echo_v2.domain.waiting_for_me import (
    PreparedAnalysis,
    WaitingForMeDecision,
    WaitingForMeResult,
)
from echo_v2.persistence.chat_repositories import (
    InMemoryAnalysisCommitRepository,
    InMemoryChatStateRepository,
    InMemoryMessageRepository,
    InMemoryWaitingForMeActiveRepository,
    InMemoryWaitingForMeResultRepository,
)
from echo_v2.ports.whatsapp import MessageDirection
from echo_v2.services.chat_analysis_worker import (
    ChatAnalysisProcessor,
    ChatAnalysisWorker,
    ConversationInput,
    RecordingAnalysisProcessor,
)

pytestmark = pytest.mark.asyncio


def _seed_chat(
    repo: InMemoryChatStateRepository,
    user_id: str = "user-1",
    chat_id: str = "972501234567@c.us",
    *,
    direction: MessageDirection = MessageDirection.INBOUND,
    next_analysis_at: datetime | None = None,
    activity_version: int = 1,
    last_processed_version: int = 0,
) -> None:
    """Directly seed a chat state in the in-memory repo."""
    from echo_v2.domain.chat import ChatState

    repo._chats[(user_id, chat_id)] = ChatState(
        user_id=user_id,
        chat_id=chat_id,
        activity_version=activity_version,
        last_message_at=datetime.now(timezone.utc),
        last_direction=direction,
        next_analysis_at=next_analysis_at,
        last_processed_version=last_processed_version,
    )


def _make_commit_repo(
    chat_state: InMemoryChatStateRepository | None = None,
) -> InMemoryAnalysisCommitRepository:
    """Build an in-memory commit repo with fresh sub-repos."""
    chat_state = chat_state or InMemoryChatStateRepository()
    return InMemoryAnalysisCommitRepository(
        chat_state_repo=chat_state,
        result_repo=InMemoryWaitingForMeResultRepository(),
        active_repo=InMemoryWaitingForMeActiveRepository(),
    )


def _make_worker(
    chat_state: InMemoryChatStateRepository,
    processor,
    commit_repo: InMemoryAnalysisCommitRepository | None = None,
    *,
    excluded_chat_ids: frozenset[str] = frozenset(),
) -> ChatAnalysisWorker:
    """Build a worker with the new commit_repo parameter."""
    if commit_repo is None:
        commit_repo = _make_commit_repo(chat_state)
    return ChatAnalysisWorker(
        chat_state_repo=chat_state,
        processor=processor,
        commit_repo=commit_repo,
        excluded_chat_ids=excluded_chat_ids,
    )


# --- Worker processes due chat, version unchanged --------------------------


async def test_worker_processes_due_chat_and_marks_processed():
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    commit_repo = _make_commit_repo(repo)
    worker = _make_worker(repo, processor, commit_repo)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    processed = await worker.run_once()
    assert processed is True
    assert len(processor.calls) == 1
    assert processor.calls[0] == ("user-1", "972501234567@c.us", 1)

    chat = await repo.get("user-1", "972501234567@c.us")
    assert chat is not None
    assert chat.last_processed_version == 1
    assert chat.next_analysis_at is None


# --- Version changed during processing --------------------------------------


async def test_worker_discards_result_when_version_changed():
    repo = InMemoryChatStateRepository()
    result_repo = InMemoryWaitingForMeResultRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    commit_repo = InMemoryAnalysisCommitRepository(repo, result_repo, active_repo)

    # A processor that simulates a new message arriving during processing
    # by incrementing the version mid-call.
    class _VersionBumpingProcessor:
        def __init__(self, repo):
            self._repo = repo
            self.calls = []

        async def process(self, user_id, chat_id, target_version):
            from echo_v2.domain.waiting_for_me import PreparedAnalysis

            self.calls.append((user_id, chat_id, target_version))
            # Simulate a new message arriving during processing
            await self._repo.upsert_on_message(
                user_id=user_id,
                chat_id=chat_id,
                direction=MessageDirection.INBOUND,
                observed_at=datetime.now(timezone.utc),
                next_analysis_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            return PreparedAnalysis(
                result=WaitingForMeResult(
                    decision=WaitingForMeDecision.WAITING_FOR_ME,
                    confidence=0.9,
                    reason="test",
                    target_version=target_version,
                ),
                conversation_snapshot={},
            )

    processor = _VersionBumpingProcessor(repo)
    worker = _make_worker(repo, processor, commit_repo)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    await worker.run_once()

    assert len(processor.calls) == 1
    chat = await repo.get("user-1", "972501234567@c.us")
    assert chat is not None
    # Version changed from 1 to 2 — commit should have returned stale
    assert chat.last_processed_version == 0
    # Chat is still due (new next_analysis_at from the bumped version)
    assert chat.next_analysis_at is not None
    assert chat.activity_version == 2

    # No result should have been persisted
    results = await result_repo.list_recent(
        user_id="user-1", chat_id="972501234567@c.us",
    )
    assert len(results) == 0

    # No active state should have been created
    active = await active_repo.get(user_id="user-1", chat_id="972501234567@c.us")
    assert active is None


# --- No due chats -----------------------------------------------------------


async def test_worker_returns_false_when_nothing_due():
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    worker = _make_worker(repo, processor)

    # No chats seeded
    processed = await worker.run_once()
    assert processed is False
    assert len(processor.calls) == 0


async def test_worker_skips_chat_not_yet_due():
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    worker = _make_worker(repo, processor)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now + timedelta(minutes=5))

    processed = await worker.run_once()
    assert processed is False
    assert len(processor.calls) == 0


async def test_worker_skips_already_processed():
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    worker = _make_worker(repo, processor)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5), last_processed_version=1)

    processed = await worker.run_once()
    assert processed is False
    assert len(processor.calls) == 0


# --- Chat disappears during processing --------------------------------------


async def test_worker_handles_disappearing_chat():
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    worker = _make_worker(repo, processor)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    # Override get to return None (simulate deletion)
    original_get = repo.get

    async def _vanishing_get(user_id, chat_id):
        return None

    repo.get = _vanishing_get
    await worker.run_once()
    repo.get = original_get

    assert len(processor.calls) == 1  # processor was called
    # No crash — just a warning log


# --- RecordingAnalysisProcessor ---------------------------------------------


async def test_recording_processor_records_calls():
    processor = RecordingAnalysisProcessor()
    await processor.process("user-1", "chat-1", 3)
    await processor.process("user-2", "chat-2", 5)
    assert processor.calls == [
        ("user-1", "chat-1", 3),
        ("user-2", "chat-2", 5),
    ]


# --- Concurrency regression test (blocking fake processor) ------------------


async def test_concurrent_message_during_processing_discards_result():
    """The core safety property: if a new message arrives during processing,
    the atomic commit must detect the version change and not persist."""
    repo = InMemoryChatStateRepository()
    result_repo = InMemoryWaitingForMeResultRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    commit_repo = InMemoryAnalysisCommitRepository(repo, result_repo, active_repo)

    # Seed a due chat.
    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    event = asyncio.Event()

    class _BlockingProcessor:
        def __init__(self):
            self.calls = []

        async def process(self, user_id, chat_id, target_version):
            from echo_v2.domain.waiting_for_me import PreparedAnalysis

            self.calls.append((user_id, chat_id, target_version))
            # Block until the test releases us.
            await event.wait()
            # By now, a new message has been ingested, bumping the version.
            return PreparedAnalysis(
                result=WaitingForMeResult(
                    decision=WaitingForMeDecision.WAITING_FOR_ME,
                    confidence=0.9,
                    reason="test",
                    target_version=target_version,
                ),
                conversation_snapshot={},
            )

    processor = _BlockingProcessor()
    worker = _make_worker(repo, processor, commit_repo)

    # Start run_once in the background.
    task = asyncio.create_task(worker.run_once())
    # Give the processor a moment to start.
    await asyncio.sleep(0.01)

    # Simulate a new inbound message arriving during processing.
    await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=datetime.now(timezone.utc),
        next_analysis_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    # Release the processor.
    event.set()
    await task

    # The worker should have called the processor once.
    assert len(processor.calls) == 1
    assert processor.calls[0][2] == 1  # target_version was 1

    # The atomic commit should have detected the version change (1 → 2)
    # and returned stale — nothing persisted.
    chat = await repo.get("user-1", "972501234567@c.us")
    assert chat is not None
    assert chat.activity_version == 2
    assert chat.last_processed_version == 0  # NOT advanced
    assert chat.next_analysis_at is not None  # still due from the new message

    # No result persisted
    results = await result_repo.list_recent(
        user_id="user-1", chat_id="972501234567@c.us",
    )
    assert len(results) == 0

    # No active state created
    active = await active_repo.get(user_id="user-1", chat_id="972501234567@c.us")
    assert active is None


# --- Processor exception leaves chat due ------------------------------------


async def test_processor_exception_leaves_chat_due():
    """If the processor raises, the chat remains due for the next poll."""
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    class _FailingProcessor:
        async def process(self, user_id, chat_id, target_version):
            raise RuntimeError("analysis failed")

    worker = _make_worker(repo, _FailingProcessor())

    # run_once should not crash — it catches exceptions per-chat.
    processed = await worker.run_once()
    assert processed is True  # there was a due chat

    # Chat should still be due — not marked processed.
    chat = await repo.get("user-1", "972501234567@c.us")
    assert chat is not None
    assert chat.last_processed_version == 0
    assert chat.next_analysis_at is not None


# --- run_loop cancellation --------------------------------------------------


async def test_run_loop_cancellation_exits_cleanly():
    """Cancelling run_loop() exits cleanly without swallowing CancelledError."""
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    worker = ChatAnalysisWorker(
        chat_state_repo=repo,
        processor=processor,
        commit_repo=_make_commit_repo(repo),
        poll_interval_seconds=0.01,
    )

    task = asyncio.create_task(worker.run_loop())
    await asyncio.sleep(0.05)  # let it poll a few times
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- list_due limit ---------------------------------------------------------


async def test_worker_processes_at_most_limit_chats():
    """run_once processes at most `limit` chats per poll (default 20)."""
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    for i in range(5):
        _seed_chat(
            repo,
            chat_id=f"chat-{i}@c.us",
            next_analysis_at=now - timedelta(minutes=5),
        )
    processor = RecordingAnalysisProcessor()
    worker = _make_worker(repo, processor)

    processed = await worker.run_once(limit=3)
    assert processed is True
    assert len(processor.calls) == 3


# --- _process_chat return value ---------------------------------------------


async def test_process_chat_returns_committed_on_success():
    """_process_chat returns 'committed' when version is still current."""
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    worker = _make_worker(repo, processor)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    chat = await repo.get("user-1", "972501234567@c.us")
    result = await worker._process_chat(chat)
    assert result == "committed"


async def test_process_chat_returns_stale_on_version_change():
    """_process_chat returns 'stale' when version changed during processing."""
    repo = InMemoryChatStateRepository()
    result_repo = InMemoryWaitingForMeResultRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    commit_repo = InMemoryAnalysisCommitRepository(repo, result_repo, active_repo)

    class _VersionBumpingProcessor:
        async def process(self, user_id, chat_id, target_version):
            from echo_v2.domain.waiting_for_me import PreparedAnalysis

            await repo.upsert_on_message(
                user_id=user_id,
                chat_id=chat_id,
                direction=MessageDirection.INBOUND,
                observed_at=datetime.now(timezone.utc),
                next_analysis_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            return PreparedAnalysis(
                result=WaitingForMeResult(
                    decision=WaitingForMeDecision.WAITING_FOR_ME,
                    confidence=0.9,
                    reason="test",
                    target_version=target_version,
                ),
                conversation_snapshot={},
            )

    processor = _VersionBumpingProcessor()
    worker = _make_worker(repo, processor, commit_repo)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    chat = await repo.get("user-1", "972501234567@c.us")
    result = await worker._process_chat(chat)
    assert result == "stale"


async def test_process_chat_returns_missing_when_chat_deleted():
    """_process_chat returns 'missing' when the chat disappears during processing."""
    from echo_v2.domain.waiting_for_me import PreparedAnalysis

    repo = InMemoryChatStateRepository()
    result_repo = InMemoryWaitingForMeResultRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    commit_repo = InMemoryAnalysisCommitRepository(repo, result_repo, active_repo)

    class _DeletingProcessor:
        async def process(self, user_id, chat_id, target_version):
            del repo._chats[(user_id, chat_id)]
            return PreparedAnalysis(
                result=WaitingForMeResult(
                    decision=WaitingForMeDecision.UNCERTAIN,
                    confidence=1.0,
                    reason="test",
                    target_version=target_version,
                ),
                conversation_snapshot={},
            )

    processor = _DeletingProcessor()
    worker = _make_worker(repo, processor, commit_repo)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    chat = await repo.get("user-1", "972501234567@c.us")
    result = await worker._process_chat(chat)
    assert result == "missing"


# --- Atomic commit: result + active in one transaction ----------------------


async def test_commit_persists_result_and_active_on_waiting_for_me():
    """When committed, both result and active state are persisted."""
    from echo_v2.domain.waiting_for_me import PreparedAnalysis

    repo = InMemoryChatStateRepository()
    result_repo = InMemoryWaitingForMeResultRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    commit_repo = InMemoryAnalysisCommitRepository(repo, result_repo, active_repo)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    outcome = await commit_repo.commit_if_current(
        user_id="user-1",
        chat_id="972501234567@c.us",
        target_version=1,
        analysis=PreparedAnalysis(
            result=WaitingForMeResult(
                decision=WaitingForMeDecision.WAITING_FOR_ME,
                confidence=0.9,
                reason="test",
                target_version=1,
            ),
            conversation_snapshot={"messages": [], "target_version": 1},
        ),
    )

    assert outcome.status == "committed"
    assert outcome.result_id is not None

    results = await result_repo.list_recent(
        user_id="user-1", chat_id="972501234567@c.us",
    )
    assert len(results) == 1
    assert results[0].decision == WaitingForMeDecision.WAITING_FOR_ME

    active = await active_repo.get(user_id="user-1", chat_id="972501234567@c.us")
    assert active is not None
    assert active.target_version == 1

    chat = await repo.get("user-1", "972501234567@c.us")
    assert chat is not None
    assert chat.last_processed_version == 1


async def test_commit_deletes_active_on_not_waiting_for_me():
    """NOT_WAITING_FOR_ME → active state deleted, result persisted."""
    from echo_v2.domain.waiting_for_me import PreparedAnalysis

    repo = InMemoryChatStateRepository()
    result_repo = InMemoryWaitingForMeResultRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    commit_repo = InMemoryAnalysisCommitRepository(repo, result_repo, active_repo)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    # Seed an existing active state
    await active_repo.upsert(
        user_id="user-1",
        chat_id="972501234567@c.us",
        target_version=1,
        result_id="old-result",
        waiting_since=now,
    )

    outcome = await commit_repo.commit_if_current(
        user_id="user-1",
        chat_id="972501234567@c.us",
        target_version=1,
        analysis=PreparedAnalysis(
            result=WaitingForMeResult(
                decision=WaitingForMeDecision.NOT_WAITING_FOR_ME,
                confidence=0.95,
                reason="closing",
                target_version=1,
            ),
            conversation_snapshot={},
        ),
    )

    assert outcome.status == "committed"

    active = await active_repo.get(user_id="user-1", chat_id="972501234567@c.us")
    assert active is None

    results = await result_repo.list_recent(
        user_id="user-1", chat_id="972501234567@c.us",
    )
    assert len(results) == 1
    assert results[0].decision == WaitingForMeDecision.NOT_WAITING_FOR_ME


async def test_commit_returns_stale_on_version_mismatch():
    """Version mismatch → nothing written."""
    from echo_v2.domain.waiting_for_me import PreparedAnalysis

    repo = InMemoryChatStateRepository()
    result_repo = InMemoryWaitingForMeResultRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    commit_repo = InMemoryAnalysisCommitRepository(repo, result_repo, active_repo)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5), activity_version=2)

    outcome = await commit_repo.commit_if_current(
        user_id="user-1",
        chat_id="972501234567@c.us",
        target_version=1,  # stale
        analysis=PreparedAnalysis(
            result=WaitingForMeResult(
                decision=WaitingForMeDecision.WAITING_FOR_ME,
                confidence=0.9,
                reason="test",
                target_version=1,
            ),
            conversation_snapshot={},
        ),
    )

    assert outcome.status == "stale"
    assert outcome.result_id is None

    results = await result_repo.list_recent(
        user_id="user-1", chat_id="972501234567@c.us",
    )
    assert len(results) == 0


async def test_commit_returns_missing_when_chat_deleted():
    """Chat disappeared → nothing written."""
    from echo_v2.domain.waiting_for_me import PreparedAnalysis

    repo = InMemoryChatStateRepository()
    result_repo = InMemoryWaitingForMeResultRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    commit_repo = InMemoryAnalysisCommitRepository(repo, result_repo, active_repo)

    outcome = await commit_repo.commit_if_current(
        user_id="user-1",
        chat_id="nonexistent@c.us",
        target_version=1,
        analysis=PreparedAnalysis(
            result=WaitingForMeResult(
                decision=WaitingForMeDecision.WAITING_FOR_ME,
                confidence=0.9,
                reason="test",
                target_version=1,
            ),
            conversation_snapshot={},
        ),
    )

    assert outcome.status == "missing"
    assert outcome.result_id is None


async def test_commit_preserves_waiting_since_on_re_analysis():
    """Re-analysis with WAITING_FOR_ME preserves waiting_since."""
    from echo_v2.domain.waiting_for_me import PreparedAnalysis

    repo = InMemoryChatStateRepository()
    result_repo = InMemoryWaitingForMeResultRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    commit_repo = InMemoryAnalysisCommitRepository(repo, result_repo, active_repo)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    # First commit
    await commit_repo.commit_if_current(
        user_id="user-1",
        chat_id="972501234567@c.us",
        target_version=1,
        analysis=PreparedAnalysis(
            result=WaitingForMeResult(
                decision=WaitingForMeDecision.WAITING_FOR_ME,
                confidence=0.9,
                reason="test",
                target_version=1,
            ),
            conversation_snapshot={},
        ),
    )

    first_active = await active_repo.get(user_id="user-1", chat_id="972501234567@c.us")
    assert first_active is not None
    first_waiting_since = first_active.waiting_since

    # Bump version (new message)
    await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=datetime.now(timezone.utc),
        next_analysis_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    # Second commit at version 2
    await commit_repo.commit_if_current(
        user_id="user-1",
        chat_id="972501234567@c.us",
        target_version=2,
        analysis=PreparedAnalysis(
            result=WaitingForMeResult(
                decision=WaitingForMeDecision.WAITING_FOR_ME,
                confidence=0.9,
                reason="test",
                target_version=2,
            ),
            conversation_snapshot={},
        ),
    )

    second_active = await active_repo.get(user_id="user-1", chat_id="972501234567@c.us")
    assert second_active is not None
    assert second_active.target_version == 2
    assert second_active.waiting_since == first_waiting_since


# --- Excluded service chats (e.g. Echo bot's own chat) ----------------------


async def test_worker_drains_excluded_chat_without_processing():
    """Excluded due chats are drained via mark_processed (no analysis run)."""
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    commit_repo = _make_commit_repo(repo)
    echo_chat_id = "972559937256@c.us"
    worker = _make_worker(
        repo,
        processor,
        commit_repo,
        excluded_chat_ids=frozenset({echo_chat_id}),
    )

    now = datetime.now(timezone.utc)
    _seed_chat(repo, chat_id=echo_chat_id, next_analysis_at=now - timedelta(minutes=5))

    processed = await worker.run_once()
    assert processed is True
    # Processor was NOT called for the excluded chat.
    assert processor.calls == []

    # The excluded chat was drained: marked processed, next_analysis_at cleared.
    chat = await repo.get("user-1", echo_chat_id)
    assert chat is not None
    assert chat.last_processed_version == 1
    assert chat.next_analysis_at is None


async def test_worker_processes_non_excluded_chat_normally():
    """Non-excluded due chats are still processed when excluded_chat_ids is set."""
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    commit_repo = _make_commit_repo(repo)
    echo_chat_id = "972559937256@c.us"
    other_chat_id = "972501234567@c.us"
    worker = _make_worker(
        repo,
        processor,
        commit_repo,
        excluded_chat_ids=frozenset({echo_chat_id}),
    )

    now = datetime.now(timezone.utc)
    _seed_chat(repo, chat_id=other_chat_id, next_analysis_at=now - timedelta(minutes=5))

    processed = await worker.run_once()
    assert processed is True
    assert len(processor.calls) == 1
    assert processor.calls[0] == ("user-1", other_chat_id, 1)


# --- Message helpers for processor edge-case tests ---------------------------


def _make_audio_msg(**kwargs) -> Message:
    defaults = dict(  # noqa: C408
        id=str(uuid.uuid4()),
        user_id="user-1",
        connection_id="conn-1",
        chat_id="972501234567@c.us",
        provider_message_id=str(uuid.uuid4()),
        direction=MessageDirection.INBOUND,
        sender_id=None,
        timestamp=datetime.now(timezone.utc),
        message_type="audio",
        text=None,
        media_download_url="https://example.com/audio.ogg",
        media_mime_type="audio/ogg",
        media_file_name="audio.ogg",
    )
    defaults.update(kwargs)
    return Message(**defaults)


def _make_media_msg(message_type: str = "image", **kwargs) -> Message:
    defaults = dict(  # noqa: C408
        id=str(uuid.uuid4()),
        user_id="user-1",
        connection_id="conn-1",
        chat_id="972501234567@c.us",
        provider_message_id=str(uuid.uuid4()),
        direction=MessageDirection.INBOUND,
        sender_id=None,
        timestamp=datetime.now(timezone.utc),
        message_type=message_type,
        text=None,
        media_download_url="https://example.com/photo.jpeg",
        media_mime_type="image/jpeg",
        media_file_name="photo.jpeg",
    )
    defaults.update(kwargs)
    return Message(**defaults)


def _make_link_msg(text: str = "https://example.com/news", **kwargs) -> Message:
    defaults = dict(  # noqa: C408
        id=str(uuid.uuid4()),
        user_id="user-1",
        connection_id="conn-1",
        chat_id="972501234567@c.us",
        provider_message_id=str(uuid.uuid4()),
        direction=MessageDirection.INBOUND,
        sender_id=None,
        timestamp=datetime.now(timezone.utc),
        message_type="text",
        text=text,
    )
    defaults.update(kwargs)
    return Message(**defaults)


# --- safe_process_chat_inputs edge cases (lines 78-83) ------------------------


async def test_safe_process_chat_inputs_returns_empty_when_chat_none():
    """safe_process_chat_inputs returns {} when chat key is missing or None."""
    from echo_v2.services.chat_analysis_worker import safe_process_chat_inputs

    assert safe_process_chat_inputs({}) == {}
    assert safe_process_chat_inputs({"chat": None}) == {}


async def test_safe_process_chat_inputs_returns_hashes_when_chat_present():
    """safe_process_chat_inputs returns hashed IDs when chat is present."""
    from echo_v2.domain.chat import ChatState
    from echo_v2.services.chat_analysis_worker import safe_process_chat_inputs

    chat = ChatState(
        user_id="user-1",
        chat_id="chat-1",
        activity_version=3,
        last_message_at=datetime.now(timezone.utc),
        last_direction=MessageDirection.INBOUND,
        next_analysis_at=None,
        last_processed_version=0,
    )
    with patch.dict(os.environ, {"OBSERVABILITY_HASH_KEY": "test-key-12345"}):
        result = safe_process_chat_inputs({"chat": chat})
    assert "user_id_hash" in result
    assert "chat_id_hash" in result
    assert result["target_version"] == 3


# --- _transcribe_audio_messages defensive None check (line 344) --------------


async def test_transcribe_audio_messages_warns_when_transcriber_none():
    """_transcribe_audio_messages logs warning when transcriber is None (defensive)."""
    repo = InMemoryMessageRepository()
    audio_msg = _make_audio_msg()
    await repo.save(audio_msg)

    analyzer = MagicMock()
    analyzer.analyze = AsyncMock()
    processor = ChatAnalysisProcessor(message_repo=repo, analyzer=analyzer)

    result = await processor._transcribe_audio_messages([audio_msg])
    # Message unchanged — text still None
    assert result[0].text is None


# --- _summarize_media_messages defensive None check (line 409) ---------------


async def test_summarize_media_messages_warns_when_summarizer_none():
    """_summarize_media_messages logs warning when summarizer is None (defensive)."""
    repo = InMemoryMessageRepository()
    image_msg = _make_media_msg(message_type="image")
    await repo.save(image_msg)

    analyzer = MagicMock()
    analyzer.analyze = AsyncMock()
    processor = ChatAnalysisProcessor(message_repo=repo, analyzer=analyzer)

    result = await processor._summarize_media_messages([image_msg])
    assert result[0].text is None


# --- _summarize_link_messages edge cases (lines 464-465, 470-471, 478) --------


async def test_summarize_link_messages_with_none_summarizer():
    """_summarize_link_messages appends message unchanged when summarizer is None."""
    repo = InMemoryMessageRepository()
    link_msg = _make_link_msg(text="https://example.com/news")
    await repo.save(link_msg)

    analyzer = MagicMock()
    analyzer.analyze = AsyncMock()
    processor = ChatAnalysisProcessor(message_repo=repo, analyzer=analyzer)

    result = await processor._summarize_link_messages([link_msg])
    assert len(result) == 1
    assert result[0].text == "https://example.com/news"


async def test_summarize_link_messages_no_urls_extracted():
    """_summarize_link_messages appends message when extract_urls returns empty."""
    repo = InMemoryMessageRepository()
    link_msg = _make_link_msg(text="https://example.com/news")
    await repo.save(link_msg)

    analyzer = MagicMock()
    analyzer.analyze = AsyncMock()

    class _StubSummarizer:
        async def summarize_link(self, url: str) -> object:
            return MagicMock(text="summary")

        async def summarize_media(self, **kwargs: object) -> object:
            return MagicMock(text="summary")

        async def close(self) -> None:
            pass

    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=_StubSummarizer()
    )

    # Patch is_link_message to True and extract_urls to empty so the
    # "no urls" branch (lines 470-471) is reached.
    with (
        patch("echo_v2.services.media_summarizer.is_link_message", return_value=True),
        patch("echo_v2.services.media_summarizer.extract_urls", return_value=[]),
    ):
        result = await processor._summarize_link_messages([link_msg])
    assert len(result) == 1
    assert result[0].text == "https://example.com/news"


async def test_summarize_link_messages_empty_summary_keeps_original():
    """When summarize_link returns empty text, new_text falls back to msg.text."""
    from echo_v2.services.media_summarizer import MediaSummary

    repo = InMemoryMessageRepository()
    link_msg = _make_link_msg(text="https://example.com/news")
    await repo.save(link_msg)

    class _EmptySummarySummarizer:
        async def summarize_link(self, url: str) -> object:
            return MediaSummary(text="", kind="link", model="fake")

        async def summarize_media(self, **kwargs: object) -> object:
            return MediaSummary(text="", kind="image", model="fake")

        async def close(self) -> None:
            pass

    analyzer = MagicMock()
    analyzer.analyze = AsyncMock()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=_EmptySummarySummarizer()
    )

    result = await processor._summarize_link_messages([link_msg])
    assert len(result) == 1
    # new_text = msg.text or urls[0] → original URL text
    assert result[0].text == "https://example.com/news"
    # Persisted via update_text
    stored = repo._messages[(link_msg.connection_id, link_msg.provider_message_id)]
    assert stored.text == "https://example.com/news"


# --- Excluded chat drain exception (lines 550-553) ---------------------------


async def test_worker_excluded_chat_drain_exception_continues():
    """If mark_processed raises for an excluded chat, run_once continues."""
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    echo_chat_id = "972559937256@c.us"
    worker = _make_worker(
        repo,
        processor,
        excluded_chat_ids=frozenset({echo_chat_id}),
    )

    now = datetime.now(timezone.utc)
    _seed_chat(repo, chat_id=echo_chat_id, next_analysis_at=now - timedelta(minutes=5))

    async def _failing_mark_processed(user_id, chat_id, version):
        raise RuntimeError("db error")

    repo.mark_processed = _failing_mark_processed

    processed = await worker.run_once()
    assert processed is True  # chat was seen/attempted
    assert processor.calls == []  # no analysis run


async def test_worker_excluded_chat_drain_cancelled_propagates():
    """CancelledError from mark_processed for an excluded chat propagates."""
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    echo_chat_id = "972559937256@c.us"
    worker = _make_worker(
        repo,
        processor,
        excluded_chat_ids=frozenset({echo_chat_id}),
    )

    now = datetime.now(timezone.utc)
    _seed_chat(repo, chat_id=echo_chat_id, next_analysis_at=now - timedelta(minutes=5))

    async def _cancelling_mark_processed(user_id, chat_id, version):
        raise asyncio.CancelledError()

    repo.mark_processed = _cancelling_mark_processed

    with pytest.raises(asyncio.CancelledError):
        await worker.run_once()


# --- CancelledError propagation (line 563) -----------------------------------


async def test_worker_propagates_cancelled_error_from_process_chat():
    """CancelledError from _process_chat propagates out of run_once."""
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    class _CancellingProcessor:
        async def process(self, user_id, chat_id, target_version):
            raise asyncio.CancelledError()

    worker = _make_worker(repo, _CancellingProcessor())

    with pytest.raises(asyncio.CancelledError):
        await worker.run_once()


# --- run_loop unexpected exception handling (lines 583-589) ------------------


async def test_run_loop_handles_unexpected_exception():
    """run_loop logs and continues when run_once raises an unexpected exception."""
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    worker = ChatAnalysisWorker(
        chat_state_repo=repo,
        processor=processor,
        commit_repo=_make_commit_repo(repo),
        poll_interval_seconds=0.01,
    )

    call_count = 0

    async def _failing_list_due(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("unexpected error")

    repo.list_due = _failing_list_due

    task = asyncio.create_task(worker.run_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert call_count >= 2  # exception was caught and loop continued


async def test_run_loop_cancelled_during_run_once():
    """run_loop handles CancelledError from run_once cleanly (lines 584-585)."""
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    worker = ChatAnalysisWorker(
        chat_state_repo=repo,
        processor=processor,
        commit_repo=_make_commit_repo(repo),
        poll_interval_seconds=0.01,
    )

    async def _cancelling_list_due(*args, **kwargs):
        raise asyncio.CancelledError()

    repo.list_due = _cancelling_list_due

    task = asyncio.create_task(worker.run_loop())
    # The CancelledError from list_due propagates through run_once to run_loop,
    # which catches, logs, and re-raises it — ending the loop.
    with pytest.raises(asyncio.CancelledError):
        await task


# --- _process_chat adds metadata when run_tree exists (line 628) -------------


async def test_process_chat_adds_metadata_when_run_tree_exists():
    """_process_chat adds user/chat metadata to the LangSmith run tree."""
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    worker = _make_worker(repo, processor)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    run_tree = MagicMock()
    run_tree.add_metadata = MagicMock()

    with (
        patch.dict(os.environ, {"OBSERVABILITY_HASH_KEY": "test-key-12345"}),
        patch("langsmith.run_helpers.get_current_run_tree", return_value=run_tree),
    ):
        chat = await repo.get("user-1", "972501234567@c.us")
        result = await worker._process_chat(chat)

    assert result == "committed"
    run_tree.add_metadata.assert_called_once()
    metadata = run_tree.add_metadata.call_args[0][0]
    assert "user_id_hash" in metadata
    assert "chat_id_hash" in metadata


# --- Judge tests (lines 658, 678-741) ----------------------------------------


class _FakeJudgeResult:
    def __init__(
        self,
        score: float = 1.0,
        explanation: str = "good",
        run_id: str = "run-123",
        run_start_time: str = "2024-01-01T00:00:00",
    ) -> None:
        self.score = score
        self.explanation = explanation
        self.run_id = run_id
        self.run_start_time = run_start_time


class _FakeJudge:
    def __init__(
        self,
        score: float = 1.0,
        explanation: str = "good",
        run_id: str = "run-123",
        run_start_time: str = "2024-01-01T00:00:00",
    ) -> None:
        self._result = _FakeJudgeResult(
            score=score,
            explanation=explanation,
            run_id=run_id,
            run_start_time=run_start_time,
        )
        self.calls: list = []

    async def judge(self, *, conversation, result):
        self.calls.append((conversation, result))
        return self._result


class _JudgeTestProcessor:
    """Processor that returns a PreparedAnalysis with conversation_input set."""

    def __init__(self) -> None:
        self.calls: list = []

    async def process(self, user_id, chat_id, target_version):
        self.calls.append((user_id, chat_id, target_version))
        return PreparedAnalysis(
            result=WaitingForMeResult(
                decision=WaitingForMeDecision.WAITING_FOR_ME,
                confidence=0.9,
                reason="test",
                target_version=target_version,
            ),
            conversation_snapshot={},
            conversation_input=ConversationInput(
                user_id=user_id,
                chat_id=chat_id,
                target_version=target_version,
                messages=[],
            ),
        )


def _make_prepared(conv_input: ConversationInput | None = None) -> PreparedAnalysis:
    return PreparedAnalysis(
        result=WaitingForMeResult(
            decision=WaitingForMeDecision.WAITING_FOR_ME,
            confidence=0.9,
            reason="test",
            target_version=1,
        ),
        conversation_snapshot={},
        conversation_input=conv_input,
    )


def _make_conv_input() -> ConversationInput:
    return ConversationInput(
        user_id="user-1",
        chat_id="chat-1",
        target_version=1,
        messages=[],
    )


async def test_run_judge_skips_when_conversation_input_none():
    """_run_judge returns early when conversation_input is None."""
    repo = InMemoryChatStateRepository()
    judge = _FakeJudge()
    worker = ChatAnalysisWorker(
        chat_state_repo=repo,
        processor=RecordingAnalysisProcessor(),
        commit_repo=_make_commit_repo(repo),
        judge=judge,
    )

    await worker._run_judge(_make_prepared(conv_input=None))
    assert judge.calls == []  # judge not called


async def test_run_judge_calls_judge_and_logs_score():
    """_run_judge calls the judge and logs the score (run_tree is None)."""
    repo = InMemoryChatStateRepository()
    judge = _FakeJudge(score=1.0, explanation="perfect")
    worker = ChatAnalysisWorker(
        chat_state_repo=repo,
        processor=RecordingAnalysisProcessor(),
        commit_repo=_make_commit_repo(repo),
        judge=judge,
    )

    with patch("langsmith.run_helpers.get_current_run_tree", return_value=None):
        await worker._run_judge(_make_prepared(conv_input=_make_conv_input()))

    assert len(judge.calls) == 1


async def test_run_judge_creates_feedback_when_run_tree_exists():
    """_run_judge creates LangSmith feedback when run_tree is not None."""
    repo = InMemoryChatStateRepository()
    judge = _FakeJudge(score=1.0, explanation="perfect", run_id="judge-run-1")
    worker = ChatAnalysisWorker(
        chat_state_repo=repo,
        processor=RecordingAnalysisProcessor(),
        commit_repo=_make_commit_repo(repo),
        judge=judge,
    )

    run_tree = MagicMock()
    run_tree.id = "analysis-run-123"

    with (
        patch("langsmith.run_helpers.get_current_run_tree", return_value=run_tree),
        patch("echo_v2.services.chat_analysis_worker.tracing_client") as mock_client,
    ):
        await worker._run_judge(_make_prepared(conv_input=_make_conv_input()))

    mock_client.create_feedback.assert_called_once()
    fb_kwargs = mock_client.create_feedback.call_args.kwargs
    assert fb_kwargs["run_id"] == "analysis-run-123"
    assert fb_kwargs["key"] == "judge_correctness"
    assert fb_kwargs["score"] == 1.0
    assert fb_kwargs["comment"] == "perfect"


async def test_run_judge_adds_to_annotation_queue_on_low_score():
    """_run_judge adds to annotation queue when score <= 0.5 and queue_id is set."""
    repo = InMemoryChatStateRepository()
    judge = _FakeJudge(score=0.0, explanation="wrong", run_id="judge-run-2")
    worker = ChatAnalysisWorker(
        chat_state_repo=repo,
        processor=RecordingAnalysisProcessor(),
        commit_repo=_make_commit_repo(repo),
        judge=judge,
    )

    run_tree = MagicMock()
    run_tree.id = "analysis-run-456"

    env = {
        "JUDGE_ANNOTATION_QUEUE_ID": "queue-123",
        "LANGSMITH_PROJECT_ID": "proj-123",
    }
    with (
        patch.dict(os.environ, env),
        patch("langsmith.run_helpers.get_current_run_tree", return_value=run_tree),
        patch("echo_v2.services.chat_analysis_worker.tracing_client") as mock_client,
    ):
        mock_client.annotation_queues.items.create = AsyncMock()
        await worker._run_judge(_make_prepared(conv_input=_make_conv_input()))

    mock_client.flush.assert_called_once()
    mock_client.annotation_queues.items.create.assert_called_once()
    create_kwargs = mock_client.annotation_queues.items.create.call_args.kwargs
    assert create_kwargs["queue_id"] == "queue-123"
    items = create_kwargs["items"]
    assert items[0]["run_id"] == "judge-run-2"
    assert items[0]["session_id"] == "proj-123"


async def test_run_judge_annotation_queue_failure_logged():
    """_run_judge logs when adding to annotation queue fails."""
    repo = InMemoryChatStateRepository()
    judge = _FakeJudge(score=0.0, explanation="wrong", run_id="judge-run-3")
    worker = ChatAnalysisWorker(
        chat_state_repo=repo,
        processor=RecordingAnalysisProcessor(),
        commit_repo=_make_commit_repo(repo),
        judge=judge,
    )

    run_tree = MagicMock()
    run_tree.id = "analysis-run-789"

    env = {
        "JUDGE_ANNOTATION_QUEUE_ID": "queue-456",
        "LANGSMITH_PROJECT_ID": "proj-456",
    }
    with (
        patch.dict(os.environ, env),
        patch("langsmith.run_helpers.get_current_run_tree", return_value=run_tree),
        patch("echo_v2.services.chat_analysis_worker.tracing_client") as mock_client,
    ):
        mock_client.annotation_queues.items.create = AsyncMock(
            side_effect=RuntimeError("queue API down")
        )
        # Should not raise — failure is logged
        await worker._run_judge(_make_prepared(conv_input=_make_conv_input()))

    mock_client.flush.assert_called_once()


async def test_run_judge_handles_exception():
    """_run_judge logs and does not raise when judge raises."""
    repo = InMemoryChatStateRepository()
    judge = MagicMock()
    judge.judge = AsyncMock(side_effect=RuntimeError("judge failed"))
    worker = ChatAnalysisWorker(
        chat_state_repo=repo,
        processor=RecordingAnalysisProcessor(),
        commit_repo=_make_commit_repo(repo),
        judge=judge,
    )

    # Should not raise
    await worker._run_judge(_make_prepared(conv_input=_make_conv_input()))


async def test_process_chat_runs_judge_on_commit():
    """_process_chat runs the judge after a successful commit."""
    repo = InMemoryChatStateRepository()
    judge = _FakeJudge(score=1.0)
    processor = _JudgeTestProcessor()
    worker = ChatAnalysisWorker(
        chat_state_repo=repo,
        processor=processor,
        commit_repo=_make_commit_repo(repo),
        judge=judge,
    )

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    chat = await repo.get("user-1", "972501234567@c.us")
    result = await worker._process_chat(chat)

    assert result == "committed"
    assert len(processor.calls) == 1
    assert len(judge.calls) == 1
