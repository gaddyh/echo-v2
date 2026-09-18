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
