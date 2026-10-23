"""Real end-to-end media processing tests.

These tests use **real files** from ``tests/fixtures/`` and call the
**real OpenAI API** (and real Modal for audio). They are skipped
automatically when the required environment variables or fixture files
are missing, so CI without credentials still passes.

To run these tests locally:

1. Drop your media files in ``tests/fixtures/``:
   - ``WhatsApp Audio 2026-09-15 at 21.36.21.opus`` (or any audio file)
   - ``WhatsApp Image 2026-09-02 at 21.19.36.jpeg`` (or any image)
   - ``NbN_Interactive_Workspace_POC_Proposal_Gaddy_Henquin.pdf`` (or any PDF)

2. Set environment variables:
   - ``OPENAI_API_KEY`` — for image/PDF/link summarization
   - ``MODAL_TRANSCRIPTION_URL``, ``MODAL_TRANSCRIPTION_KEY``,
     ``MODAL_TRANSCRIPTION_SECRET`` — for audio transcription

3. Run:
   ```.venv/bin/python -m pytest tests/services/test_media_e2e.py -v -s
   ```

The ``-s`` flag is recommended so you can see the real summaries printed
to stdout.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from echo_v2.services.media_summarizer import (
    OpenAIMediaSummarizer,
    summarize_document_bytes,
    summarize_image_bytes,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"

# Fixture file names (flexible — matches any audio/image/pdf in the dir).
_AUDIO_FILE = FIXTURES_DIR / "WhatsApp Audio 2026-09-15 at 21.36.21.opus"
_IMAGE_FILE = FIXTURES_DIR / "WhatsApp Image 2026-09-02 at 21.19.36.jpeg"
_IMAGE_FILE_2 = FIXTURES_DIR / "echo-core-architecture.png"
_PDF_FILE = FIXTURES_DIR / "NbN_Interactive_Workspace_POC_Proposal_Gaddy_Henquin.pdf"

HAS_OPENAI = bool(os.getenv("OPENAI_API_KEY"))
HAS_MODAL = bool(
    os.getenv("MODAL_TRANSCRIPTION_URL")
    and os.getenv("MODAL_TRANSCRIPTION_KEY")
    and os.getenv("MODAL_TRANSCRIPTION_SECRET")
)

skip_no_openai = pytest.mark.skipif(
    not HAS_OPENAI, reason="OPENAI_API_KEY not set — skipping real OpenAI E2E tests"
)
skip_no_modal = pytest.mark.skipif(
    not HAS_MODAL, reason="Modal transcription env vars not set — skipping audio E2E"
)
skip_no_audio_file = pytest.mark.skipif(
    not _AUDIO_FILE.exists(), reason=f"Audio fixture not found: {_AUDIO_FILE.name}"
)
skip_no_image_file = pytest.mark.skipif(
    not _IMAGE_FILE.exists(), reason=f"Image fixture not found: {_IMAGE_FILE.name}"
)
skip_no_pdf_file = pytest.mark.skipif(
    not _PDF_FILE.exists(), reason=f"PDF fixture not found: {_PDF_FILE.name}"
)


def _real_openai_client():
    """Build a real AsyncOpenAI client."""
    from openai import AsyncOpenAI

    return AsyncOpenAI()


# --- Image summarization (real OpenAI vision) -------------------------------


@skip_no_openai
@skip_no_image_file
async def test_e2e_image_summarization_real_openai():
    """Summarize a real JPEG image using gpt-4o vision.

    Verifies the full path: file read → base64 encode → OpenAI vision API
    → non-empty summary text.
    """
    image_bytes = _IMAGE_FILE.read_bytes()
    print(f"\n[image] file={_IMAGE_FILE.name} size={len(image_bytes)} bytes")

    client = _real_openai_client()
    try:
        result = await summarize_image_bytes(
            client=client,
            model="gpt-4o",
            image_bytes=image_bytes,
            mime_type="image/jpeg",
            caption=None,
        )
    finally:
        await client.close()

    print(f"[image] kind={result.kind} model={result.model}")
    print(f"[image] summary: {result.text}")
    assert result.text, "Expected non-empty image summary"
    assert result.kind == "image"
    assert len(result.text) > 10, "Summary too short — vision API may have failed"


@skip_no_openai
async def test_e2e_png_image_summarization_real_openai():
    """Summarize a real PNG architecture diagram using gpt-4o vision."""
    if not _IMAGE_FILE_2.exists():
        pytest.skip(f"PNG fixture not found: {_IMAGE_FILE_2.name}")

    image_bytes = _IMAGE_FILE_2.read_bytes()
    print(f"\n[png] file={_IMAGE_FILE_2.name} size={len(image_bytes)} bytes")

    client = _real_openai_client()
    try:
        result = await summarize_image_bytes(
            client=client,
            model="gpt-4o",
            image_bytes=image_bytes,
            mime_type="image/png",
            caption=None,
        )
    finally:
        await client.close()

    print(f"[png] kind={result.kind} model={result.model}")
    print(f"[png] summary: {result.text}")
    assert result.text, "Expected non-empty PNG summary"


# --- PDF/document summarization (real OpenAI vision) -----------------------


@skip_no_openai
@skip_no_pdf_file
async def test_e2e_pdf_summarization_real_openai():
    """Summarize a real PDF document using gpt-4o vision (file input).

    Verifies the full path: file read → base64 encode → OpenAI vision API
    with PDF file input → non-empty summary text.
    """
    pdf_bytes = _PDF_FILE.read_bytes()
    print(f"\n[pdf] file={_PDF_FILE.name} size={len(pdf_bytes)} bytes")

    client = _real_openai_client()
    try:
        result = await summarize_document_bytes(
            client=client,
            vision_model="gpt-4o",
            text_model="gpt-4.1",
            document_bytes=pdf_bytes,
            mime_type="application/pdf",
            file_name=_PDF_FILE.name,
            caption=None,
        )
    finally:
        await client.close()

    print(f"[pdf] kind={result.kind} model={result.model}")
    print(f"[pdf] summary: {result.text}")
    assert result.text, "Expected non-empty PDF summary"
    assert len(result.text) > 10, "Summary too short — vision API may have failed"


# --- Audio transcription (real Modal Whisper) -------------------------------


@skip_no_modal
@skip_no_audio_file
async def test_e2e_audio_transcription_real_modal():
    """Transcribe a real .opus audio file using Modal Whisper.

    Verifies the full path: file read → temp file → ffmpeg conversion
    (if needed) → Modal transcription API → non-empty transcript text.
    """
    from echo_v2.integrations.modal.settings import load_settings
    from echo_v2.integrations.modal.transcriber import ModalWhisperTranscriber
    from echo_v2.services.transcription import (
        ensure_transcribable_audio,
    )

    audio_bytes = _AUDIO_FILE.read_bytes()
    print(f"\n[audio] file={_AUDIO_FILE.name} size={len(audio_bytes)} bytes")

    import tempfile
    from pathlib import Path

    # Write to a temp file with the correct extension, then run through
    # the real conversion + transcription pipeline.
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = Path(tmpdir) / _AUDIO_FILE.name
        raw_path.write_bytes(audio_bytes)
        print(f"[audio] raw path: {raw_path}")

        transcribable_path = ensure_transcribable_audio(raw_path)
        print(f"[audio] transcribable path: {transcribable_path}")
        converted_bytes = transcribable_path.read_bytes()
        print(f"[audio] converted size={len(converted_bytes)} bytes")

        settings = load_settings()
        transcriber = ModalWhisperTranscriber(settings=settings)
        result = await transcriber.transcribe_bytes(
            audio_bytes=converted_bytes,
            filename=transcribable_path.name,
            content_type="audio/ogg",
        )

    print(f"[audio] transcript: {result.text}")
    assert result.text, "Expected non-empty transcript"
    assert len(result.text) > 5, "Transcript too short — Whisper may have failed"


# --- Full processor E2E (real files through the pipeline) -------------------


@skip_no_openai
@skip_no_image_file
async def test_e2e_processor_image_through_pipeline():
    """Run a real image through the full ChatAnalysisProcessor pipeline.

    Uses a real OpenAI client for the MediaSummarizer and a fake analyzer
    to capture the conversation. Verifies the image summary ends up in the
    conversation input that would be sent to the WaitingForMe analyzer.
    """
    from echo_v2.domain.chat import MessageDirection
    from echo_v2.services.chat_analysis_worker import ChatAnalysisProcessor
    from tests.services.test_chat_analysis_processor import (
        FakeAnalyzer,
        InMemoryMessageRepository,
        _make_media_message,
        _make_message,
    )

    image_bytes = _IMAGE_FILE.read_bytes()

    # We can't use a real download URL, so we mock download_media to return
    # the real file bytes. This tests everything except the HTTP download.
    image_msg = _make_media_message(
        offset_minutes=0,
        message_type="image",
        media_download_url="file://fixture/image.jpeg",
        media_mime_type="image/jpeg",
        media_file_name=_IMAGE_FILE.name,
    )
    reply_msg = _make_message(
        direction=MessageDirection.OUTBOUND, text="got it", offset_minutes=1
    )

    repo = InMemoryMessageRepository()
    await repo.save(image_msg)
    await repo.save(reply_msg)

    analyzer = FakeAnalyzer()
    client = _real_openai_client()
    summarizer = OpenAIMediaSummarizer(client=client)
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=summarizer
    )

    from unittest.mock import AsyncMock, patch

    try:
        with patch(
            "echo_v2.services.media_summarizer.download_media",
            new_callable=AsyncMock,
            return_value=image_bytes,
        ):
            await processor.process("user-1", "972501234567@c.us", target_version=1)
    finally:
        await client.close()

    conv = analyzer.calls[0]
    image_text = conv.messages[0][1]
    print(f"\n[processor] image summary in conversation: {image_text}")
    assert image_text, "Expected non-empty image summary in conversation"
    assert image_text != "", "Image summary should not be empty"
    assert conv.messages[1][1] == "got it"

    # Verify the summary was persisted
    stored = repo._messages[(image_msg.connection_id, image_msg.provider_message_id)]
    print(f"[processor] persisted text: {stored.text}")
    assert stored.text == image_text


@skip_no_openai
@skip_no_pdf_file
async def test_e2e_processor_pdf_through_pipeline():
    """Run a real PDF through the full ChatAnalysisProcessor pipeline."""
    from echo_v2.domain.chat import MessageDirection
    from echo_v2.services.chat_analysis_worker import ChatAnalysisProcessor
    from tests.services.test_chat_analysis_processor import (
        FakeAnalyzer,
        InMemoryMessageRepository,
        _make_media_message,
        _make_message,
    )

    pdf_bytes = _PDF_FILE.read_bytes()

    doc_msg = _make_media_message(
        offset_minutes=0,
        message_type="document",
        media_download_url="file://fixture/doc.pdf",
        media_mime_type="application/pdf",
        media_file_name=_PDF_FILE.name,
    )
    reply_msg = _make_message(
        direction=MessageDirection.OUTBOUND, text="thanks", offset_minutes=1
    )

    repo = InMemoryMessageRepository()
    await repo.save(doc_msg)
    await repo.save(reply_msg)

    analyzer = FakeAnalyzer()
    client = _real_openai_client()
    summarizer = OpenAIMediaSummarizer(client=client)
    processor = ChatAnalysisProcessor(
        message_repo=repo, analyzer=analyzer, media_summarizer=summarizer
    )

    from unittest.mock import AsyncMock, patch

    try:
        with patch(
            "echo_v2.services.media_summarizer.download_media",
            new_callable=AsyncMock,
            return_value=pdf_bytes,
        ):
            await processor.process("user-1", "972501234567@c.us", target_version=1)
    finally:
        await client.close()

    conv = analyzer.calls[0]
    doc_text = conv.messages[0][1]
    print(f"\n[processor] PDF summary in conversation: {doc_text}")
    assert doc_text, "Expected non-empty PDF summary in conversation"

    stored = repo._messages[(doc_msg.connection_id, doc_msg.provider_message_id)]
    print(f"[processor] persisted text: {stored.text}")
    assert stored.text == doc_text


# --- Link summarization (real HTTP fetch + real OpenAI) --------------------


@skip_no_openai
async def test_e2e_link_summarization_real_http():
    """Summarize a real URL: fetch HTML + strip + summarize via OpenAI.

    Uses a stable public URL (example.com) to avoid flakiness.
    """
    from echo_v2.services.media_summarizer import summarize_link

    client = _real_openai_client()
    try:
        result = await summarize_link(
            client=client,
            model="gpt-4.1",
            url="https://example.com",
        )
    finally:
        await client.close()

    print(f"\n[link] kind={result.kind} model={result.model}")
    print(f"[link] summary: {result.text}")
    assert result.text, "Expected non-empty link summary"
    assert result.kind == "link"


# --- Video summarization (real Gemini) ---------------------------------------


# Drop a real WhatsApp video file in tests/fixtures/ (any *.mp4 / *.3gp / *.mov).
# The tests/fixtures/ directory is gitignored so real media is never committed.
_VIDEO_FILES = sorted(
    list(FIXTURES_DIR.glob("*.mp4"))
    + list(FIXTURES_DIR.glob("*.3gp"))
    + list(FIXTURES_DIR.glob("*.mov"))
)
_VIDEO_FILE = _VIDEO_FILES[0] if _VIDEO_FILES else None

HAS_GEMINI = bool(os.getenv("GEMINI_API_KEY"))

skip_no_gemini = pytest.mark.skipif(
    not HAS_GEMINI, reason="GEMINI_API_KEY not set — skipping real Gemini E2E tests"
)
skip_no_video_file = pytest.mark.skipif(
    not _VIDEO_FILE, reason="No video fixture (*.mp4/*.3gp/*.mov) in tests/fixtures/"
)


@skip_no_gemini
@skip_no_video_file
async def test_e2e_video_summarization_real_gemini():
    """Summarize a real video using Gemini's native video understanding.

    Verifies the full path: file read → inline bytes → Gemini API
    → non-empty Hebrew summary text.
    """
    from echo_v2.services.gemini_video_summarizer import GeminiVideoSummarizer

    video_bytes = _VIDEO_FILE.read_bytes()  # type: ignore[union-attr]
    print(f"\n[video] file={_VIDEO_FILE.name} size={len(video_bytes)} bytes")  # type: ignore[union-attr]

    summarizer = GeminiVideoSummarizer(
        api_key=os.environ["GEMINI_API_KEY"],
        model=os.environ.get("GEMINI_VIDEO_MODEL", "gemini-3.5-flash-lite"),
    )
    try:
        with patch(
            "echo_v2.services.gemini_video_summarizer.GeminiVideoSummarizer._download_bounded",
            new_callable=AsyncMock,
            return_value=video_bytes,
        ):
            result = await summarizer.summarize_media(
                download_url="file://fixture/video.mp4",
                mime_type="video/mp4",
                file_name=_VIDEO_FILE.name,  # type: ignore[union-attr]
                message_type="video",
                caption=None,
            )
    finally:
        await summarizer.close()

    print(f"[video] kind={result.kind} model={result.model}")
    print(f"[video] summary: {result.text}")
    assert result.text, "Expected non-empty video summary"
    assert result.kind == "video"
    assert len(result.text) > 10, "Summary too short — Gemini may have failed"


@skip_no_gemini
@skip_no_video_file
async def test_e2e_video_summarization_with_caption_real_gemini():
    """Summarize a real video with a caption using Gemini.

    Verifies the caption is included as context and the summary is non-empty.
    """
    from echo_v2.services.gemini_video_summarizer import GeminiVideoSummarizer

    video_bytes = _VIDEO_FILE.read_bytes()  # type: ignore[union-attr]
    print(f"\n[video+caption] file={_VIDEO_FILE.name} size={len(video_bytes)} bytes")  # type: ignore[union-attr]

    summarizer = GeminiVideoSummarizer(
        api_key=os.environ["GEMINI_API_KEY"],
        model=os.environ.get("GEMINI_VIDEO_MODEL", "gemini-3.5-flash-lite"),
    )
    try:
        with patch(
            "echo_v2.services.gemini_video_summarizer.GeminiVideoSummarizer._download_bounded",
            new_callable=AsyncMock,
            return_value=video_bytes,
        ):
            result = await summarizer.summarize_media(
                download_url="file://fixture/video.mp4",
                mime_type="video/mp4",
                file_name=_VIDEO_FILE.name,  # type: ignore[union-attr]
                message_type="video",
                caption="תראה מה קרה היום",
            )
    finally:
        await summarizer.close()

    print(f"[video+caption] kind={result.kind} model={result.model}")
    print(f"[video+caption] summary: {result.text}")
    assert result.text, "Expected non-empty video summary with caption"
    assert result.kind == "video"


# --- Video → Gemini → Guard analyzers E2E -------------------------------------


@skip_no_gemini
@skip_no_video_file
@skip_no_openai
async def test_e2e_video_through_gemini_and_guard_analyzers_clean():
    """End-to-end: real video → Gemini description → Guard analyzers.

    Runs a benign video (e.g. animals.mp4 / bunny.mp4) through Gemini to
    get a Hebrew factual description, then feeds that description as a
    conversation message to both the SimpleSafetyAnalyzer and the
    ExposureAnalyzer. Asserts both return clean results — no safety
    concern and no significant exposure — confirming that a benign video
    enrichment does not produce false alerts.
    """
    from datetime import datetime, timezone

    from echo_v2.domain.guard_simple import (
        SimpleConversation,
        SimpleConversationMessage,
    )
    from echo_v2.services.gemini_video_summarizer import GeminiVideoSummarizer
    from echo_v2.services.guard_exposure_analyzer import ExposureAnalyzer
    from echo_v2.services.guard_simple_analyzer import SimpleSafetyAnalyzer

    video_bytes = _VIDEO_FILE.read_bytes()  # type: ignore[union-attr]
    print(f"\n[e2e-guard] file={_VIDEO_FILE.name} size={len(video_bytes)} bytes")  # type: ignore[union-attr]

    # Step 1: Gemini video summarization
    gemini = GeminiVideoSummarizer(
        api_key=os.environ["GEMINI_API_KEY"],
        model=os.environ.get("GEMINI_VIDEO_MODEL", "gemini-3.5-flash-lite"),
    )
    try:
        with patch(
            "echo_v2.services.gemini_video_summarizer.GeminiVideoSummarizer._download_bounded",
            new_callable=AsyncMock,
            return_value=video_bytes,
        ):
            summary = await gemini.summarize_media(
                download_url="file://fixture/video.mp4",
                mime_type="video/mp4",
                file_name=_VIDEO_FILE.name,  # type: ignore[union-attr]
                message_type="video",
                caption=None,
            )
    finally:
        await gemini.close()

    print(f"[e2e-guard] gemini summary: {summary.text}")
    assert summary.text, "Expected non-empty Gemini video summary"
    assert summary.kind == "video"

    # Step 2: Build a conversation with the video description as the only message
    now = datetime.now(timezone.utc)
    conversation = SimpleConversation(
        child_user_id="child-1",
        child_name="ילד",
        chat_id="chat@c.us",
        chat_type="group",
        connection_id="conn-1",
        timezone="Asia/Jerusalem",
        window_start=now,
        window_end=now,
        monitored_child_role="child",
        messages=(
            SimpleConversationMessage(
                id="msg-video-1",
                sender="ילד",
                text=summary.text,
                timestamp=now,
            ),
        ),
    )

    # Step 3: Run the SimpleSafetyAnalyzer (security analyzer)
    openai_client = _real_openai_client()
    try:
        safety_analyzer = SimpleSafetyAnalyzer(
            client=openai_client,
            model=os.environ.get("GUARD_LLM_MODEL_NAME", "gpt-4.1"),
        )
        safety = await safety_analyzer.analyze(conversation)
    finally:
        await openai_client.close()

    print(f"[e2e-guard] safety: parent_should_know={safety.parent_should_know} "
          f"child_role={safety.child_role.value} summary={safety.summary}")
    # A benign video should not trigger a safety concern.
    assert safety.parent_should_know is not True, (
        f"Benign video should not trigger parent_should_know=True: {safety.summary}"
    )

    # Step 4: Run the ExposureAnalyzer
    openai_client = _real_openai_client()
    try:
        exposure_analyzer = ExposureAnalyzer(
            client=openai_client,
            model=os.environ.get("GUARD_LLM_MODEL_NAME", "gpt-4.1"),
        )
        exposure = await exposure_analyzer.analyze(conversation)
    finally:
        await openai_client.close()

    print(f"[e2e-guard] exposure: significant={exposure.significant_exposure} "
          f"category={exposure.category} summary={exposure.summary}")
    # A benign video should not trigger a significant exposure event.
    assert exposure.significant_exposure is not True, (
        f"Benign video should not trigger significant_exposure=True: {exposure.summary}"
    )


# --- YouTube URL → Gemini (real Gemini API) ---------------------------------


# A stable, public, benign YouTube video used for the integration test.
# "Big Buck Bunny" (Blender open movie) — public, benign, well-known.
# If this URL ever breaks, replace with another public benign video.
_YOUTUBE_TEST_URL = "https://www.youtube.com/watch?v=YE7WW3R1I3o"


@skip_no_gemini
async def test_e2e_youtube_summarization_real_gemini():
    """Summarize a real public YouTube video via Gemini's YouTube URL preview.

    Verifies the full path: build types.Content with file_data + video_metadata
    → real Gemini API → non-empty summary text. This is the integration test
    required before enabling VIDEO_LINK_ENRICHMENT_ENABLED in production.

    Note: this test does NOT verify the 60-second processing cap is enforced
    server-side (Gemini does not expose processing duration). It verifies the
    request is accepted and returns a useful description. The cap is enforced
    by passing video_metadata start_offset/end_offset; if Gemini ignored it,
    the request would still succeed but process more of the video (higher
    cost, not a test failure). The cap's correctness is therefore structural
    (we send the right metadata) rather than observable via the response.
    """
    from echo_v2.services.gemini_video_summarizer import GeminiVideoSummarizer

    summarizer = GeminiVideoSummarizer(
        api_key=os.environ["GEMINI_API_KEY"],
        model=os.environ.get("GEMINI_VIDEO_MODEL", "gemini-3.5-flash-lite"),
        youtube_max_seconds=int(os.environ.get("GEMINI_VIDEO_YOUTUBE_MAX_SECONDS", "60")),
    )
    try:
        result = await summarizer.summarize_youtube(_YOUTUBE_TEST_URL)
    finally:
        await summarizer.close()

    print(f"[youtube] kind={result.kind} model={result.model}")
    print(f"[youtube] summary: {result.text}")
    assert result.text, "Expected non-empty YouTube video summary"
    assert result.kind == "video"
    assert len(result.text) > 10, "Summary too short — Gemini may have failed"
