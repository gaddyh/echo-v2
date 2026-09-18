"""Tests for ChatAnalysisProcessor — message loading + analyzer (stage 2).

The processor now returns a PreparedAnalysis without persisting.
Persistence is handled by AnalysisCommitRepository (tested in
test_chat_analysis_worker.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.chat import Message
from echo_v2.domain.waiting_for_me import (
    WaitingForMeDecision,
    WaitingForMeResult,
)
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
    prepared = await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert len(analyzer.calls) == 1
    conv = analyzer.calls[0]
    assert conv.target_version == 1
    assert len(conv.messages) == 3
    assert conv.messages[0] == ("inbound", "hello", msgs[0].timestamp)
    assert conv.messages[1] == ("outbound", "hi there", msgs[1].timestamp)
    assert conv.messages[2] == ("inbound", "how are you?", msgs[2].timestamp)

    # Returns PreparedAnalysis
    assert prepared.result.decision == WaitingForMeDecision.WAITING_FOR_ME
    assert prepared.result.target_version == 1
    assert prepared.conversation_snapshot is not None
    assert prepared.conversation_snapshot["target_version"] == 1
    assert len(prepared.conversation_snapshot["messages"]) == 3


async def test_processor_handles_empty_chat():
    """process() on a chat with no messages still calls the analyzer."""
    repo = InMemoryMessageRepository()
    analyzer = FakeAnalyzer(decision=WaitingForMeDecision.UNCERTAIN)
    processor = ChatAnalysisProcessor(message_repo=repo, analyzer=analyzer)
    prepared = await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert len(analyzer.calls) == 1
    assert analyzer.calls[0].messages == []
    assert prepared.result.decision == WaitingForMeDecision.UNCERTAIN


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


async def test_processor_returns_prepared_analysis_with_snapshot():
    """process() returns a PreparedAnalysis with conversation snapshot."""
    repo = InMemoryMessageRepository()
    msgs = [
        _make_message(direction=MessageDirection.INBOUND, text="hello", offset_minutes=0),
        _make_message(direction=MessageDirection.OUTBOUND, text="hi", offset_minutes=1),
        _make_message(direction=MessageDirection.INBOUND, text="what's up?", offset_minutes=2),
    ]
    for m in msgs:
        await repo.save(m)

    analyzer = FakeAnalyzer(WaitingForMeDecision.WAITING_FOR_ME)
    processor = ChatAnalysisProcessor(message_repo=repo, analyzer=analyzer)
    prepared = await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert prepared.result.decision == WaitingForMeDecision.WAITING_FOR_ME
    assert prepared.result.target_version == 1
    assert prepared.conversation_snapshot["target_version"] == 1
    assert len(prepared.conversation_snapshot["messages"]) == 3
    assert prepared.conversation_snapshot["messages"][0]["direction"] == "inbound"
    assert prepared.conversation_snapshot["messages"][0]["text"] == "hello"


async def test_processor_does_not_persist():
    """The processor does not persist — it returns PreparedAnalysis."""
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
    processor = ChatAnalysisProcessor(message_repo=repo, analyzer=analyzer)
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    # Nothing persisted — just returned
    result_repo = InMemoryWaitingForMeResultRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()

    # The processor has no result_repo or active_repo
    assert not hasattr(processor, "_result_repo") or processor._result_repo is None
    assert not hasattr(processor, "_active_repo") or processor._active_repo is None

    results = await result_repo.list_recent(user_id="user-1", chat_id="972501234567@c.us")
    assert len(results) == 0

    active = await active_repo.get(user_id="user-1", chat_id="972501234567@c.us")
    assert active is None


# --- Audio transcription tests -----------------------------------------------


def _make_audio_message(
    *,
    direction: MessageDirection = MessageDirection.INBOUND,
    offset_minutes: int = 0,
    media_download_url: str = "https://example.com/audio.ogg",
    media_mime_type: str = "audio/ogg",
    media_file_name: str = "voice-message.ogg",
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
        message_type="audio",
        text=None,
        media_download_url=media_download_url,
        media_mime_type=media_mime_type,
        media_file_name=media_file_name,
    )


def _make_media_message(
    *,
    direction: MessageDirection = MessageDirection.INBOUND,
    offset_minutes: int = 0,
    message_type: str = "image",
    media_download_url: str = "https://example.com/photo.jpeg",
    media_mime_type: str = "image/jpeg",
    media_file_name: str = "photo.jpeg",
    text: str | None = None,
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
        message_type=message_type,
        text=text,
        media_download_url=media_download_url,
        media_mime_type=media_mime_type,
        media_file_name=media_file_name,
    )


class FakeTranscriber:
    """Fake Transcriber that returns a fixed transcript and records calls."""

    def __init__(self, transcript: str = "transcribed text") -> None:
        self.transcript = transcript
        self.calls: list[dict] = []

    async def transcribe_bytes(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
    ) -> object:
        from echo_v2.services.transcription import TranscriptionResult

        self.calls.append(
            {"filename": filename, "content_type": content_type, "bytes_len": len(audio_bytes)}
        )
        return TranscriptionResult(text=self.transcript, model="fake")

    async def close(self) -> None:
        pass


async def test_processor_transcribes_audio_messages_before_analysis():
    """Audio messages with download_url are transcribed before analysis."""
    from unittest.mock import AsyncMock, patch

    repo = InMemoryMessageRepository()
    audio_msg = _make_audio_message(offset_minutes=0)
    text_msg = _make_message(direction=MessageDirection.OUTBOUND, text="reply", offset_minutes=1)
    await repo.save(audio_msg)
    await repo.save(text_msg)

    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, transcriber=object()  # non-None to enable
    )
    with patch(
        "echo_v2.services.chat_analysis_worker.handle_direct_audio_download_url",
        new_callable=AsyncMock,
        return_value="שלום עולם",
    ) as mock_helper:
        await processor.process("user-1", "972501234567@c.us", target_version=1)

    # Transcription helper was called once (for the audio message)
    assert mock_helper.await_count == 1
    _, kwargs = mock_helper.call_args
    assert kwargs["download_url"] == "https://example.com/audio.ogg"
    assert kwargs["file_name"] == "voice-message.ogg"
    assert kwargs["mime_type"] == "audio/ogg"

    # The analyzer received the transcript as text
    conv = analyzer.calls[0]
    assert conv.messages[0][1] == "שלום עולם"
    assert conv.messages[1][1] == "reply"

    # The transcript was persisted via update_text
    updated_msg = repo._messages[(audio_msg.connection_id, audio_msg.provider_message_id)]
    assert updated_msg.text == "שלום עולם"


async def test_processor_with_no_transcriber_uses_empty_text_for_audio():
    """Without a transcriber, audio messages use text='' (same as today)."""
    repo = InMemoryMessageRepository()
    audio_msg = _make_audio_message(offset_minutes=0)
    await repo.save(audio_msg)

    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(message_repo=repo, analyzer=analyzer)
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    # No transcription attempted
    conv = analyzer.calls[0]
    assert conv.messages[0][1] == ""  # text or "" → ""

    # Message text is still None in the repo
    stored = repo._messages[(audio_msg.connection_id, audio_msg.provider_message_id)]
    assert stored.text is None


async def test_processor_transcription_failure_uses_empty_text():
    """If transcription raises, the audio message uses text='' and no crash."""
    from unittest.mock import AsyncMock, patch

    repo = InMemoryMessageRepository()
    audio_msg = _make_audio_message(offset_minutes=0)
    await repo.save(audio_msg)

    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, transcriber=object()  # non-None to enable
    )
    with patch(
        "echo_v2.services.chat_analysis_worker.handle_direct_audio_download_url",
        new_callable=AsyncMock,
        side_effect=RuntimeError("Modal timed out"),
    ):
        await processor.process("user-1", "972501234567@c.us", target_version=1)

    # Analyzer still called, audio message has empty text
    conv = analyzer.calls[0]
    assert conv.messages[0][1] == ""

    # Message text is still None in the repo (update_text not called)
    stored = repo._messages[(audio_msg.connection_id, audio_msg.provider_message_id)]
    assert stored.text is None


async def test_processor_skips_transcription_for_text_messages():
    """Text messages are not sent to the transcriber."""
    from unittest.mock import AsyncMock, patch

    repo = InMemoryMessageRepository()
    text_msg = _make_message(direction=MessageDirection.INBOUND, text="hello", offset_minutes=0)
    await repo.save(text_msg)

    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, transcriber=object()
    )
    with patch(
        "echo_v2.services.chat_analysis_worker.handle_direct_audio_download_url",
        new_callable=AsyncMock,
    ) as mock_helper:
        await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert mock_helper.await_count == 0
    assert analyzer.calls[0].messages[0][1] == "hello"


async def test_processor_skips_audio_with_no_download_url():
    """Audio messages without a download_url are not transcribed."""
    from unittest.mock import AsyncMock, patch

    repo = InMemoryMessageRepository()
    audio_msg = _make_audio_message(
        offset_minutes=0, media_download_url=None, media_mime_type=None, media_file_name=None
    )
    await repo.save(audio_msg)

    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, transcriber=object()
    )
    with patch(
        "echo_v2.services.chat_analysis_worker.handle_direct_audio_download_url",
        new_callable=AsyncMock,
    ) as mock_helper:
        await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert mock_helper.await_count == 0
    assert analyzer.calls[0].messages[0][1] == ""


# --- Media summarization tests -----------------------------------------------


class FakeMediaSummarizer:
    """Fake MediaSummarizer that returns a fixed summary and records calls."""

    def __init__(self, summary: str = "a photo of a cat") -> None:
        self.summary = summary
        self.calls: list[dict] = []

    async def summarize_media(
        self,
        *,
        download_url: str,
        mime_type: str | None,
        file_name: str | None,
        message_type: str,
        caption: str | None = None,
    ) -> object:
        from echo_v2.services.media_summarizer import MediaSummary

        self.calls.append(
            {
                "download_url": download_url,
                "mime_type": mime_type,
                "file_name": file_name,
                "message_type": message_type,
                "caption": caption,
            }
        )
        return MediaSummary(text=self.summary, kind=message_type, model="fake")

    async def summarize_link(self, url: str) -> object:
        from echo_v2.services.media_summarizer import MediaSummary

        return MediaSummary(text=f"[link: {url}]", kind="link", model="fake")

    async def close(self) -> None:
        pass


async def test_processor_summarizes_image_messages_before_analysis():
    """Image messages with download_url are summarized before analysis."""
    repo = InMemoryMessageRepository()
    image_msg = _make_media_message(offset_minutes=0, message_type="image")
    text_msg = _make_message(direction=MessageDirection.OUTBOUND, text="reply", offset_minutes=1)
    await repo.save(image_msg)
    await repo.save(text_msg)

    analyzer = FakeAnalyzer()
    summarizer = FakeMediaSummarizer(summary="a photo of a cat")
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=summarizer
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    # Summarizer was called once for the image
    assert len(summarizer.calls) == 1
    call = summarizer.calls[0]
    assert call["download_url"] == "https://example.com/photo.jpeg"
    assert call["mime_type"] == "image/jpeg"
    assert call["file_name"] == "photo.jpeg"
    assert call["message_type"] == "image"

    # The analyzer received the summary as text
    conv = analyzer.calls[0]
    assert conv.messages[0][1] == "a photo of a cat"
    assert conv.messages[1][1] == "reply"

    # The summary was persisted via update_text
    updated_msg = repo._messages[(image_msg.connection_id, image_msg.provider_message_id)]
    assert updated_msg.text == "a photo of a cat"


async def test_processor_summarizes_document_messages_before_analysis():
    """Document messages with download_url are summarized before analysis."""
    repo = InMemoryMessageRepository()
    doc_msg = _make_media_message(
        offset_minutes=0,
        message_type="document",
        media_download_url="https://example.com/report.pdf",
        media_mime_type="application/pdf",
        media_file_name="report.pdf",
    )
    await repo.save(doc_msg)

    analyzer = FakeAnalyzer()
    summarizer = FakeMediaSummarizer(summary="a quarterly report")
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=summarizer
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert len(summarizer.calls) == 1
    assert summarizer.calls[0]["message_type"] == "document"
    assert analyzer.calls[0].messages[0][1] == "a quarterly report"


async def test_processor_with_no_summarizer_uses_empty_text_for_media():
    """Without a summarizer, media messages use text=''."""
    repo = InMemoryMessageRepository()
    image_msg = _make_media_message(offset_minutes=0, message_type="image")
    await repo.save(image_msg)

    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(message_repo=repo, analyzer=analyzer)
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    conv = analyzer.calls[0]
    assert conv.messages[0][1] == ""

    # Message text is still None in the repo
    stored = repo._messages[(image_msg.connection_id, image_msg.provider_message_id)]
    assert stored.text is None


async def test_processor_summarization_failure_uses_empty_text():
    """If summarization raises, the media message uses text='' and no crash."""

    class FailingSummarizer:
        async def summarize_media(self, **kwargs: object) -> object:
            raise RuntimeError("vision API down")

        async def summarize_link(self, url: str) -> object:
            raise RuntimeError("vision API down")

        async def close(self) -> None:
            pass

    repo = InMemoryMessageRepository()
    image_msg = _make_media_message(offset_minutes=0, message_type="image")
    await repo.save(image_msg)

    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=FailingSummarizer()
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    conv = analyzer.calls[0]
    assert conv.messages[0][1] == ""

    stored = repo._messages[(image_msg.connection_id, image_msg.provider_message_id)]
    assert stored.text is None


async def test_processor_skips_summarization_for_text_messages():
    """Text messages are not sent to the summarizer."""
    repo = InMemoryMessageRepository()
    text_msg = _make_message(direction=MessageDirection.INBOUND, text="hello", offset_minutes=0)
    await repo.save(text_msg)

    analyzer = FakeAnalyzer()
    summarizer = FakeMediaSummarizer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=summarizer
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert len(summarizer.calls) == 0
    assert analyzer.calls[0].messages[0][1] == "hello"


async def test_processor_skips_media_with_no_download_url():
    """Media messages without a download_url are not summarized."""
    repo = InMemoryMessageRepository()
    image_msg = _make_media_message(
        offset_minutes=0,
        message_type="image",
        media_download_url=None,
        media_mime_type=None,
        media_file_name=None,
    )
    await repo.save(image_msg)

    analyzer = FakeAnalyzer()
    summarizer = FakeMediaSummarizer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=summarizer
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert len(summarizer.calls) == 0
    assert analyzer.calls[0].messages[0][1] == ""


async def test_processor_skips_media_with_existing_text():
    """Media messages that already have text are not re-summarized."""
    repo = InMemoryMessageRepository()
    image_msg = _make_media_message(
        offset_minutes=0, message_type="image", text="already captioned"
    )
    await repo.save(image_msg)

    analyzer = FakeAnalyzer()
    summarizer = FakeMediaSummarizer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=summarizer
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert len(summarizer.calls) == 0
    assert analyzer.calls[0].messages[0][1] == "already captioned"


# --- Link summarization tests -----------------------------------------------


class FakeLinkSummarizer:
    """Fake MediaSummarizer that records link calls and returns a fixed summary."""

    def __init__(self, link_summary: str = "an article about AI") -> None:
        self.link_summary = link_summary
        self.link_calls: list[str] = []
        self.media_calls: list[dict] = []

    async def summarize_media(
        self,
        *,
        download_url: str,
        mime_type: str | None,
        file_name: str | None,
        message_type: str,
        caption: str | None = None,
    ) -> object:
        from echo_v2.services.media_summarizer import MediaSummary

        self.media_calls.append(
            {"download_url": download_url, "message_type": message_type}
        )
        return MediaSummary(text="media summary", kind=message_type, model="fake")

    async def summarize_link(self, url: str) -> object:
        from echo_v2.services.media_summarizer import MediaSummary

        self.link_calls.append(url)
        return MediaSummary(text=self.link_summary, kind="link", model="fake")

    async def close(self) -> None:
        pass


async def test_processor_summarizes_link_only_text_message():
    """A text message that is just a URL is summarized."""
    repo = InMemoryMessageRepository()
    link_msg = _make_message(
        direction=MessageDirection.INBOUND,
        text="https://example.com/news-article",
        offset_minutes=0,
    )
    await repo.save(link_msg)

    analyzer = FakeAnalyzer()
    summarizer = FakeLinkSummarizer(link_summary="breaking news about tech")
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=summarizer
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert len(summarizer.link_calls) == 1
    assert summarizer.link_calls[0] == "https://example.com/news-article"

    # The analyzer sees the URL + summary
    conv = analyzer.calls[0]
    assert "https://example.com/news-article" in conv.messages[0][1]
    assert "breaking news about tech" in conv.messages[0][1]

    # The summary was persisted
    stored = repo._messages[(link_msg.connection_id, link_msg.provider_message_id)]
    assert "breaking news about tech" in stored.text


async def test_processor_summarizes_short_text_with_url():
    """A short text message wrapping a URL is summarized."""
    repo = InMemoryMessageRepository()
    link_msg = _make_message(
        direction=MessageDirection.INBOUND,
        text="check this: https://example.com/article",
        offset_minutes=0,
    )
    await repo.save(link_msg)

    analyzer = FakeAnalyzer()
    summarizer = FakeLinkSummarizer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=summarizer
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert len(summarizer.link_calls) == 1


async def test_processor_does_not_summarize_long_text_with_url():
    """Long conversational text with a URL is not summarized."""
    long_text = (
        "hey i was thinking about the project and whether we should "
        "look at https://example.com for reference but let me know"
    )
    repo = InMemoryMessageRepository()
    text_msg = _make_message(
        direction=MessageDirection.INBOUND, text=long_text, offset_minutes=0
    )
    await repo.save(text_msg)

    analyzer = FakeAnalyzer()
    summarizer = FakeLinkSummarizer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=summarizer
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert len(summarizer.link_calls) == 0
    assert analyzer.calls[0].messages[0][1] == long_text


async def test_processor_link_summarization_failure_keeps_original_text():
    """If link summarization fails, the original text is kept."""

    class FailingLinkSummarizer:
        async def summarize_media(self, **kwargs: object) -> object:
            from echo_v2.services.media_summarizer import MediaSummary

            return MediaSummary(text="media", kind="image", model="fake")

        async def summarize_link(self, url: str) -> object:
            raise RuntimeError("fetch failed")

        async def close(self) -> None:
            pass

    repo = InMemoryMessageRepository()
    link_msg = _make_message(
        direction=MessageDirection.INBOUND,
        text="https://example.com/news",
        offset_minutes=0,
    )
    await repo.save(link_msg)

    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=FailingLinkSummarizer()
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    # Original URL text is preserved
    assert analyzer.calls[0].messages[0][1] == "https://example.com/news"


async def test_processor_no_summarizer_skips_link_summarization():
    """Without a summarizer, link messages keep their original text."""
    repo = InMemoryMessageRepository()
    link_msg = _make_message(
        direction=MessageDirection.INBOUND,
        text="https://example.com/news",
        offset_minutes=0,
    )
    await repo.save(link_msg)

    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(message_repo=repo, analyzer=analyzer)
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert analyzer.calls[0].messages[0][1] == "https://example.com/news"
