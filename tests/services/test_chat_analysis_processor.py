"""Tests for ChatAnalysisProcessor — message loading + analyzer (stage 2)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.chat import Message
from echo_v2.domain.waiting_for_me import WaitingForMeDecision, WaitingForMeResult
from echo_v2.persistence.chat_repositories import InMemoryMessageRepository
from echo_v2.ports.whatsapp import MessageDirection
from echo_v2.services.chat_analysis_worker import (
    ChatAnalysisProcessor,
    ConversationInput,
)

pytestmark = pytest.mark.asyncio

BASE_TIME = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


class FakeAnalyzer:
    """Records the ConversationInput it receives and returns a fixed result."""

    def __init__(
        self,
        decision: WaitingForMeDecision = WaitingForMeDecision.WAITING_FOR_ME,
    ) -> None:
        self.calls: list[ConversationInput] = []
        self._decision = decision

    async def analyze(self, conversation: ConversationInput) -> WaitingForMeResult:
        self.calls.append(conversation)
        return WaitingForMeResult(
            decision=self._decision,
            confidence=0.9,
            reason="test reason",
            target_version=conversation.target_version,
        )


def _make_message(
    *,
    direction: MessageDirection,
    text: str,
    offset_minutes: int,
    user_id: str = "user-1",
    chat_id: str = "972501234567@c.us",
) -> Message:
    return Message(
        id=str(uuid.uuid4()),
        user_id=user_id,
        connection_id="conn-1",
        chat_id=chat_id,
        provider_message_id=str(uuid.uuid4()),
        direction=direction,
        sender_id=None,
        timestamp=BASE_TIME + timedelta(minutes=offset_minutes),
        message_type="text",
        text=text,
    )


async def test_processor_loads_messages_and_calls_analyzer():
    """process() loads messages and passes them to the analyzer."""
    repo = InMemoryMessageRepository()
    msgs = [
        _make_message(direction=MessageDirection.INBOUND, text="hello", offset_minutes=0),
        _make_message(direction=MessageDirection.OUTBOUND, text="hi there", offset_minutes=1),
        _make_message(direction=MessageDirection.INBOUND, text="how are you?", offset_minutes=2),
    ]
    for m in msgs:
        await repo.save(m)

    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(message_repo=repo, analyzer=analyzer)
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert len(analyzer.calls) == 1
    conv = analyzer.calls[0]
    assert conv.target_version == 1
    assert len(conv.messages) == 3
    assert conv.messages[0] == ("inbound", "hello", msgs[0].timestamp)
    assert conv.messages[1] == ("outbound", "hi there", msgs[1].timestamp)
    assert conv.messages[2] == ("inbound", "how are you?", msgs[2].timestamp)


async def test_processor_handles_empty_chat():
    """process() on a chat with no messages still calls the analyzer."""
    repo = InMemoryMessageRepository()
    analyzer = FakeAnalyzer(decision=WaitingForMeDecision.UNCERTAIN)
    processor = ChatAnalysisProcessor(message_repo=repo, analyzer=analyzer)
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert len(analyzer.calls) == 1
    assert analyzer.calls[0].messages == []


async def test_processor_respects_context_messages_param():
    """context_messages controls how many messages before the last outbound are loaded."""
    repo = InMemoryMessageRepository()
    msgs = [
        _make_message(direction=MessageDirection.INBOUND, text=f"pre-{i}", offset_minutes=i)
        for i in range(10)
    ] + [
        _make_message(direction=MessageDirection.OUTBOUND, text="reply", offset_minutes=10),
        _make_message(direction=MessageDirection.INBOUND, text="after", offset_minutes=11),
    ]
    for m in msgs:
        await repo.save(m)

    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(message_repo=repo, analyzer=analyzer, context_messages=2)
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    # 2 context + 1 outbound + 1 after = 4
    assert len(analyzer.calls[0].messages) == 4


async def test_processor_respects_max_no_outbound_param():
    """max_no_outbound limits messages when there's no outbound in the chat."""
    repo = InMemoryMessageRepository()
    msgs = [
        _make_message(direction=MessageDirection.INBOUND, text=f"msg-{i}", offset_minutes=i)
        for i in range(30)
    ]
    for m in msgs:
        await repo.save(m)

    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, max_no_outbound=10,
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert len(analyzer.calls[0].messages) == 10


async def test_processor_satisfies_analysis_processor_protocol():
    """ChatAnalysisProcessor satisfies the AnalysisProcessor protocol."""
    from echo_v2.services.chat_analysis_worker import AnalysisProcessor

    repo = InMemoryMessageRepository()
    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(message_repo=repo, analyzer=analyzer)
    assert isinstance(processor, AnalysisProcessor)


async def test_processor_passes_target_version_to_analyzer():
    """The target_version from process() flows through to the analyzer."""
    repo = InMemoryMessageRepository()
    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(message_repo=repo, analyzer=analyzer)
    await processor.process("user-1", "972501234567@c.us", target_version=42)

    assert analyzer.calls[0].target_version == 42


# --- Result storage (stage 3) -----------------------------------------------


async def test_processor_stores_result_when_result_repo_provided():
    """When a result_repo is provided, the processor saves the result."""
    from echo_v2.persistence.chat_repositories import (
        InMemoryWaitingForMeResultRepository,
    )

    repo = InMemoryMessageRepository()
    msgs = [
        _make_message(direction=MessageDirection.INBOUND, text="hello", offset_minutes=0),
        _make_message(direction=MessageDirection.OUTBOUND, text="hi", offset_minutes=1),
        _make_message(direction=MessageDirection.INBOUND, text="what's up?", offset_minutes=2),
    ]
    for m in msgs:
        await repo.save(m)

    analyzer = FakeAnalyzer(WaitingForMeDecision.WAITING_FOR_ME)
    result_repo = InMemoryWaitingForMeResultRepository()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, result_repo=result_repo,
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    stored = await result_repo.list_recent(
        user_id="user-1", chat_id="972501234567@c.us",
    )
    assert len(stored) == 1
    assert stored[0].decision == WaitingForMeDecision.WAITING_FOR_ME
    assert stored[0].target_version == 1


async def test_processor_does_not_store_when_result_repo_is_none():
    """When result_repo is None, the processor does not persist."""
    repo = InMemoryMessageRepository()
    msgs = [
        _make_message(direction=MessageDirection.INBOUND, text="hello", offset_minutes=0),
    ]
    for m in msgs:
        await repo.save(m)

    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, result_repo=None,
    )
    # Should not raise
    await processor.process("user-1", "972501234567@c.us", target_version=1)


# --- Active state management (stage 4) --------------------------------------


async def test_processor_creates_active_on_waiting_for_me():
    """WAITING_FOR_ME → upsert active state."""
    from echo_v2.persistence.chat_repositories import (
        InMemoryWaitingForMeActiveRepository,
        InMemoryWaitingForMeResultRepository,
    )

    repo = InMemoryMessageRepository()
    msgs = [
        _make_message(direction=MessageDirection.INBOUND, text="hello", offset_minutes=0),
    ]
    for m in msgs:
        await repo.save(m)

    analyzer = FakeAnalyzer(WaitingForMeDecision.WAITING_FOR_ME)
    result_repo = InMemoryWaitingForMeResultRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    processor = ChatAnalysisProcessor(
        message_repo=repo,
        analyzer=analyzer,
        result_repo=result_repo,
        active_repo=active_repo,
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    active = await active_repo.get(user_id="user-1", chat_id="972501234567@c.us")
    assert active is not None
    assert active.target_version == 1
    assert active.waiting_since is not None
    assert active.notified_at is None


async def test_processor_deletes_active_on_not_waiting_for_me():
    """NOT_WAITING_FOR_ME → delete active state if exists."""
    from echo_v2.persistence.chat_repositories import (
        InMemoryWaitingForMeActiveRepository,
        InMemoryWaitingForMeResultRepository,
    )

    repo = InMemoryMessageRepository()
    msgs = [
        _make_message(direction=MessageDirection.INBOUND, text="thanks", offset_minutes=0),
    ]
    for m in msgs:
        await repo.save(m)

    analyzer = FakeAnalyzer(WaitingForMeDecision.NOT_WAITING_FOR_ME)
    result_repo = InMemoryWaitingForMeResultRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    processor = ChatAnalysisProcessor(
        message_repo=repo,
        analyzer=analyzer,
        result_repo=result_repo,
        active_repo=active_repo,
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    active = await active_repo.get(user_id="user-1", chat_id="972501234567@c.us")
    assert active is None


async def test_processor_deletes_active_on_uncertain():
    """UNCERTAIN → delete active state if exists."""
    from echo_v2.persistence.chat_repositories import (
        InMemoryWaitingForMeActiveRepository,
        InMemoryWaitingForMeResultRepository,
    )

    repo = InMemoryMessageRepository()
    msgs = [
        _make_message(direction=MessageDirection.INBOUND, text="", offset_minutes=0),
    ]
    for m in msgs:
        await repo.save(m)

    analyzer = FakeAnalyzer(WaitingForMeDecision.UNCERTAIN)
    result_repo = InMemoryWaitingForMeResultRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    processor = ChatAnalysisProcessor(
        message_repo=repo,
        analyzer=analyzer,
        result_repo=result_repo,
        active_repo=active_repo,
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    active = await active_repo.get(user_id="user-1", chat_id="972501234567@c.us")
    assert active is None


async def test_processor_preserves_waiting_since_on_re_analysis():
    """Re-analysis with WAITING_FOR_ME preserves waiting_since from first analysis."""
    from echo_v2.persistence.chat_repositories import (
        InMemoryWaitingForMeActiveRepository,
        InMemoryWaitingForMeResultRepository,
    )

    repo = InMemoryMessageRepository()
    msgs = [
        _make_message(direction=MessageDirection.INBOUND, text="send me the file?", offset_minutes=0),
    ]
    for m in msgs:
        await repo.save(m)

    analyzer = FakeAnalyzer(WaitingForMeDecision.WAITING_FOR_ME)
    result_repo = InMemoryWaitingForMeResultRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    processor = ChatAnalysisProcessor(
        message_repo=repo,
        analyzer=analyzer,
        result_repo=result_repo,
        active_repo=active_repo,
    )

    # First analysis — creates active
    await processor.process("user-1", "972501234567@c.us", target_version=1)
    first_active = await active_repo.get(user_id="user-1", chat_id="972501234567@c.us")
    assert first_active is not None
    first_waiting_since = first_active.waiting_since

    # Second analysis — new version, should preserve waiting_since
    await processor.process("user-1", "972501234567@c.us", target_version=2)
    second_active = await active_repo.get(user_id="user-1", chat_id="972501234567@c.us")
    assert second_active is not None
    assert second_active.target_version == 2
    assert second_active.waiting_since == first_waiting_since


async def test_processor_no_active_repo_does_not_crash():
    """Processor works without active_repo — just stores result."""
    from echo_v2.persistence.chat_repositories import (
        InMemoryWaitingForMeResultRepository,
    )

    repo = InMemoryMessageRepository()
    msgs = [
        _make_message(direction=MessageDirection.INBOUND, text="hello", offset_minutes=0),
    ]
    for m in msgs:
        await repo.save(m)

    analyzer = FakeAnalyzer(WaitingForMeDecision.WAITING_FOR_ME)
    result_repo = InMemoryWaitingForMeResultRepository()
    processor = ChatAnalysisProcessor(
        message_repo=repo,
        analyzer=analyzer,
        result_repo=result_repo,
        active_repo=None,  # no active repo
    )
    # Should not raise
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    stored = await result_repo.list_recent(user_id="user-1", chat_id="972501234567@c.us")
    assert len(stored) == 1
