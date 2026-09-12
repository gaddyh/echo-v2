"""ChatAnalysisWorker tests (in-memory repos, no Docker needed)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.waiting_for_me import (
    WaitingForMeDecision,
    WaitingForMeResult,
)
from echo_v2.persistence.chat_repositories import (
    InMemoryAnalysisCommitRepository,
    InMemoryChatStateRepository,
    InMemoryWaitingForMeActiveRepository,
    InMemoryWaitingForMeResultRepository,
)
from echo_v2.ports.whatsapp import MessageDirection
from echo_v2.services.chat_analysis_worker import (
    ChatAnalysisWorker,
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
) -> ChatAnalysisWorker:
    """Build a worker with the new commit_repo parameter."""
    if commit_repo is None:
        commit_repo = _make_commit_repo(chat_state)
    return ChatAnalysisWorker(
        chat_state_repo=chat_state,
        processor=processor,
        commit_repo=commit_repo,
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
