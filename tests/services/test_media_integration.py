"""Integration-style tests exercising all media types through ChatAnalysisProcessor.

This file demonstrates how to test every media type (audio, image, video,
document, and link) in a single end-to-end flow through the analysis
processor. Each media type uses a fake provider so no real downloads or
LLM calls are made.

The pattern for testing each media type:

1. **Audio** — provide a ``Transcriber`` fake; assert the transcript
   appears in the conversation input and is persisted via ``update_text``.
2. **Image** — provide a ``MediaSummarizer`` fake; assert the vision
   summary appears in the conversation input.
3. **Video** — same as image; the summarizer returns a placeholder.
4. **Document** — same as image; the summarizer returns a text/PDF summary.
5. **Link** — a text message containing a URL; the summarizer's
   ``summarize_link`` is called and the summary replaces the text.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from echo_v2.domain.chat import MessageDirection
from echo_v2.services.chat_analysis_worker import ChatAnalysisProcessor
from echo_v2.services.media_summarizer import MediaSummary
from echo_v2.services.transcription import Transcriber, TranscriptionResult
from tests.services.test_chat_analysis_processor import (
    FakeAnalyzer,
    InMemoryMessageRepository,
    _make_audio_message,
    _make_media_message,
    _make_message,
)


class FakeTranscriber(Transcriber):
    """Fake transcriber returning a fixed transcript."""

    def __init__(self, transcript: str = "hello world") -> None:
        self.transcript = transcript
        self.calls: list[bytes] = []

    async def transcribe_bytes(
        self, audio_bytes: bytes, *, mime_type: str = "", file_name: str = ""
    ) -> TranscriptionResult:
        self.calls.append(audio_bytes)
        return TranscriptionResult(text=self.transcript)


class FakeMediaSummarizer:
    """Fake summarizer that returns per-type summaries and records calls."""

    def __init__(self) -> None:
        self.media_calls: list[dict] = []
        self.link_calls: list[str] = []
        # Per-type summaries to make assertions distinguishable.
        self._media_summaries = {
            "image": "a photo of a sunset",
            "video": "[video: clip.mp4]",
            "document": "a quarterly financial report",
        }

    async def summarize_media(
        self,
        *,
        download_url: str,
        mime_type: str | None,
        file_name: str | None,
        message_type: str,
        caption: str | None = None,
    ) -> MediaSummary:
        self.media_calls.append(
            {
                "download_url": download_url,
                "mime_type": mime_type,
                "file_name": file_name,
                "message_type": message_type,
                "caption": caption,
            }
        )
        text = self._media_summaries.get(message_type, f"[{message_type}]")
        return MediaSummary(text=text, kind=message_type, model="fake")

    async def summarize_link(self, url: str) -> MediaSummary:
        self.link_calls.append(url)
        return MediaSummary(text="an article about AI breakthroughs", kind="link", model="fake")

    async def close(self) -> None:
        pass


async def test_all_media_types_in_one_conversation():
    """Exercise audio, image, video, document, and link in one conversation.

    This is the canonical "test all media types" test. It builds a
    conversation with one message of each type plus a text reply, then
    verifies each media type is processed correctly:

    - Audio → transcribed
    - Image → summarized via vision
    - Video → placeholder summary
    - Document → summarized via text/vision
    - Link (text message with URL) → link summary
    """
    repo = InMemoryMessageRepository()

    # One message of each media type, all inbound.
    audio_msg = _make_audio_message(offset_minutes=0)
    image_msg = _make_media_message(
        offset_minutes=1,
        message_type="image",
        media_download_url="https://example.com/photo.jpeg",
        media_mime_type="image/jpeg",
        media_file_name="photo.jpeg",
    )
    video_msg = _make_media_message(
        offset_minutes=2,
        message_type="video",
        media_download_url="https://example.com/clip.mp4",
        media_mime_type="video/mp4",
        media_file_name="clip.mp4",
    )
    doc_msg = _make_media_message(
        offset_minutes=3,
        message_type="document",
        media_download_url="https://example.com/report.pdf",
        media_mime_type="application/pdf",
        media_file_name="report.pdf",
    )
    link_msg = _make_message(
        direction=MessageDirection.INBOUND,
        text="https://example.com/news-article",
        offset_minutes=4,
    )
    # An outbound reply so the analyzer has context.
    reply_msg = _make_message(
        direction=MessageDirection.OUTBOUND, text="thanks!", offset_minutes=5
    )

    for msg in (audio_msg, image_msg, video_msg, doc_msg, link_msg, reply_msg):
        await repo.save(msg)

    transcriber = FakeTranscriber(transcript="the audio transcript")
    summarizer = FakeMediaSummarizer()
    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(
        message_repo=repo,
        analyzer=analyzer,
        transcriber=transcriber,
        media_summarizer=summarizer,
    )

    # Patch the audio download helper so it doesn't make real HTTP calls.
    with patch(
        "echo_v2.services.chat_analysis_worker.handle_direct_audio_download_url",
        new_callable=AsyncMock,
        return_value="the audio transcript",
    ) as mock_audio_helper:
        await processor.process("user-1", "972501234567@c.us", target_version=1)

    # --- Audio assertions ---
    assert mock_audio_helper.await_count == 1
    # --- Media summarizer assertions ---
    assert len(summarizer.media_calls) == 3  # image, video, document
    media_types = sorted(c["message_type"] for c in summarizer.media_calls)
    assert media_types == ["document", "image", "video"]
    # --- Link summarizer assertions ---
    assert len(summarizer.link_calls) == 1
    assert summarizer.link_calls[0] == "https://example.com/news-article"

    # --- Conversation input assertions ---
    conv = analyzer.calls[0]
    texts = [t for _, t, _ in conv.messages]
    assert texts[0] == "the audio transcript"  # audio
    assert texts[1] == "a photo of a sunset"  # image
    assert texts[2] == "[video: clip.mp4]"  # video
    assert texts[3] == "a quarterly financial report"  # document
    assert "https://example.com/news-article" in texts[4]  # link
    assert "an article about AI breakthroughs" in texts[4]  # link summary
    assert texts[5] == "thanks!"  # plain text reply

    # --- Persistence assertions ---
    stored_audio = repo._messages[(audio_msg.connection_id, audio_msg.provider_message_id)]
    assert stored_audio.text == "the audio transcript"
    stored_image = repo._messages[(image_msg.connection_id, image_msg.provider_message_id)]
    assert stored_image.text == "a photo of a sunset"
    stored_link = repo._messages[(link_msg.connection_id, link_msg.provider_message_id)]
    assert "an article about AI breakthroughs" in stored_link.text


async def test_audio_only_with_transcriber_no_summarizer():
    """Audio works with just a transcriber, no media summarizer."""
    repo = InMemoryMessageRepository()
    audio_msg = _make_audio_message(offset_minutes=0)
    reply = _make_message(direction=MessageDirection.OUTBOUND, text="ok", offset_minutes=1)
    await repo.save(audio_msg)
    await repo.save(reply)

    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, transcriber=FakeTranscriber()
    )
    with patch(
        "echo_v2.services.chat_analysis_worker.handle_direct_audio_download_url",
        new_callable=AsyncMock,
        return_value="transcribed text",
    ):
        await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert analyzer.calls[0].messages[0][1] == "transcribed text"


async def test_image_only_with_summarizer_no_transcriber():
    """Image works with just a summarizer, no transcriber."""
    repo = InMemoryMessageRepository()
    image_msg = _make_media_message(offset_minutes=0, message_type="image")
    reply = _make_message(direction=MessageDirection.OUTBOUND, text="nice", offset_minutes=1)
    await repo.save(image_msg)
    await repo.save(reply)

    analyzer = FakeAnalyzer()
    summarizer = FakeMediaSummarizer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=summarizer
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert len(summarizer.media_calls) == 1
    assert analyzer.calls[0].messages[0][1] == "a photo of a sunset"


async def test_link_only_with_summarizer():
    """Link summarization works standalone."""
    repo = InMemoryMessageRepository()
    link_msg = _make_message(
        direction=MessageDirection.INBOUND,
        text="https://example.com/article",
        offset_minutes=0,
    )
    await repo.save(link_msg)

    analyzer = FakeAnalyzer()
    summarizer = FakeMediaSummarizer()
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=summarizer
    )
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    assert len(summarizer.link_calls) == 1
    assert "an article about AI breakthroughs" in analyzer.calls[0].messages[0][1]


async def test_no_providers_uses_empty_text_for_media():
    """Without transcriber or summarizer, all media uses text=''."""
    repo = InMemoryMessageRepository()
    audio_msg = _make_audio_message(offset_minutes=0)
    image_msg = _make_media_message(offset_minutes=1, message_type="image")
    await repo.save(audio_msg)
    await repo.save(image_msg)

    analyzer = FakeAnalyzer()
    processor = ChatAnalysisProcessor(message_repo=repo, analyzer=analyzer)
    await processor.process("user-1", "972501234567@c.us", target_version=1)

    conv = analyzer.calls[0]
    assert conv.messages[0][1] == ""  # audio
    assert conv.messages[1][1] == ""  # image
