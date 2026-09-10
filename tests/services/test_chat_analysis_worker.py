"""ChatAnalysisWorker tests (in-memory repos, no Docker needed)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.persistence.chat_repositories import InMemoryChatStateRepository
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


# --- Worker processes due chat, version unchanged --------------------------


async def test_worker_processes_due_chat_and_marks_processed():
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    worker = ChatAnalysisWorker(chat_state_repo=repo, processor=processor)

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
    # A processor that simulates a new message arriving during processing
    # by incrementing the version mid-call.
    class _VersionBumpingProcessor:
        def __init__(self, repo):
            self._repo = repo
            self.calls = []

        async def process(self, user_id, chat_id, target_version):
            self.calls.append((user_id, chat_id, target_version))
            # Simulate a new message arriving during processing
            await self._repo.upsert_on_message(
                user_id=user_id,
                chat_id=chat_id,
                direction=MessageDirection.INBOUND,
                observed_at=datetime.now(timezone.utc),
                next_analysis_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )

    processor = _VersionBumpingProcessor(repo)
    worker = ChatAnalysisWorker(chat_state_repo=repo, processor=processor)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    await worker.run_once()

    assert len(processor.calls) == 1
    chat = await repo.get("user-1", "972501234567@c.us")
    assert chat is not None
    # Version changed from 1 to 2 — mark_processed should NOT have succeeded
    assert chat.last_processed_version == 0
    # Chat is still due (new next_analysis_at from the bumped version)
    assert chat.next_analysis_at is not None
    assert chat.activity_version == 2


# --- No due chats -----------------------------------------------------------


async def test_worker_returns_false_when_nothing_due():
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    worker = ChatAnalysisWorker(chat_state_repo=repo, processor=processor)

    # No chats seeded
    processed = await worker.run_once()
    assert processed is False
    assert len(processor.calls) == 0


async def test_worker_skips_chat_not_yet_due():
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    worker = ChatAnalysisWorker(chat_state_repo=repo, processor=processor)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now + timedelta(minutes=5))

    processed = await worker.run_once()
    assert processed is False
    assert len(processor.calls) == 0


async def test_worker_skips_already_processed():
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    worker = ChatAnalysisWorker(chat_state_repo=repo, processor=processor)

    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5), last_processed_version=1)

    processed = await worker.run_once()
    assert processed is False
    assert len(processor.calls) == 0


# --- Chat disappears during processing --------------------------------------


async def test_worker_handles_disappearing_chat():
    repo = InMemoryChatStateRepository()
    processor = RecordingAnalysisProcessor()
    worker = ChatAnalysisWorker(chat_state_repo=repo, processor=processor)

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
    the worker's mark_processed must fail (version changed)."""
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)

    # Seed a due chat.
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    event = asyncio.Event()

    class _BlockingProcessor:
        def __init__(self):
            self.calls = []

        async def process(self, user_id, chat_id, target_version):
            self.calls.append((user_id, chat_id, target_version))
            # Block until the test releases us.
            await event.wait()
            # By now, a new message has been ingested, bumping the version.

    processor = _BlockingProcessor()
    worker = ChatAnalysisWorker(chat_state_repo=repo, processor=processor)

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

    # mark_processed should have failed (version changed from 1 to 2).
    chat = await repo.get("user-1", "972501234567@c.us")
    assert chat is not None
    assert chat.activity_version == 2
    assert chat.last_processed_version == 0  # NOT advanced
    assert chat.next_analysis_at is not None  # still due from the new message


# --- Processor exception leaves chat due ------------------------------------


async def test_processor_exception_leaves_chat_due():
    """If the processor raises, the chat remains due for the next poll."""
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    _seed_chat(repo, next_analysis_at=now - timedelta(minutes=5))

    class _FailingProcessor:
        async def process(self, user_id, chat_id, target_version):
            raise RuntimeError("analysis failed")

    worker = ChatAnalysisWorker(chat_state_repo=repo, processor=_FailingProcessor())

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
        chat_state_repo=repo, processor=processor, poll_interval_seconds=0.01
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
    worker = ChatAnalysisWorker(chat_state_repo=repo, processor=processor)

    processed = await worker.run_once(limit=3)
    assert processed is True
    assert len(processor.calls) == 3
