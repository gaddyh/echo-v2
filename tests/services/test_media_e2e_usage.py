"""Real E2E usage recording tests — validates the full PR 1 cost ledger.

These tests call **real provider APIs** (OpenAI, Gemini, Modal) while a real
``AiUsageRecorder`` backed by a Postgres testcontainer persists usage events.
After each provider call, the test queries ``ai_usage_events`` and prints the
recorded rows — provider, model, tokens/audio duration, cost source, and
estimated USD cost — so we can visually validate that the cost ledger works
end-to-end against real provider responses.

Requirements:
- Docker (for the Postgres testcontainer)
- ``OPENAI_API_KEY`` for OpenAI calls
- ``GEMINI_API_KEY`` for Gemini calls
- ``MODAL_TRANSCRIPTION_*`` for Modal calls
- Fixture files in ``tests/fixtures/``

Run:
    set -a && source .env && set +a
    .venv/bin/python -m pytest tests/services/test_media_e2e_usage.py -v -s
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from echo_v2.persistence.orm import AiUsageEventRow
from echo_v2.services.reporting.ai_usage_recorder import (
    AiUsageRecorder,
)
from echo_v2.services.reporting.pricing_service import PricingService

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
_AUDIO_FILE = FIXTURES_DIR / "WhatsApp Audio 2026-09-15 at 21.36.21.opus"
_IMAGE_FILE = FIXTURES_DIR / "WhatsApp Image 2026-09-02 at 21.19.36.jpeg"
_PDF_FILE = FIXTURES_DIR / "NbN_Interactive_Workspace_POC_Proposal_Gaddy_Henquin.pdf"
_VIDEO_FILES = sorted(
    list(FIXTURES_DIR.glob("*.mp4"))
    + list(FIXTURES_DIR.glob("*.3gp"))
    + list(FIXTURES_DIR.glob("*.mov"))
)
_VIDEO_FILE = _VIDEO_FILES[0] if _VIDEO_FILES else None

HAS_OPENAI = bool(os.getenv("OPENAI_API_KEY"))
HAS_GEMINI = bool(os.getenv("GEMINI_API_KEY"))
HAS_MODAL = bool(
    os.getenv("MODAL_TRANSCRIPTION_URL")
    and os.getenv("MODAL_TRANSCRIPTION_KEY")
    and os.getenv("MODAL_TRANSCRIPTION_SECRET")
)

skip_no_openai = pytest.mark.skipif(
    not HAS_OPENAI, reason="OPENAI_API_KEY not set"
)
skip_no_gemini = pytest.mark.skipif(
    not HAS_GEMINI, reason="GEMINI_API_KEY not set"
)
skip_no_modal = pytest.mark.skipif(
    not HAS_MODAL, reason="Modal transcription env vars not set"
)
skip_no_image = pytest.mark.skipif(
    not _IMAGE_FILE.exists(), reason=f"Image fixture not found: {_IMAGE_FILE.name}"
)
skip_no_pdf = pytest.mark.skipif(
    not _PDF_FILE.exists(), reason=f"PDF fixture not found: {_PDF_FILE.name}"
)
skip_no_audio = pytest.mark.skipif(
    not _AUDIO_FILE.exists(), reason=f"Audio fixture not found: {_AUDIO_FILE.name}"
)
skip_no_video = pytest.mark.skipif(
    not _VIDEO_FILE, reason="No video fixture (*.mp4/*.3gp/*.mov) in tests/fixtures/"
)


# --- Postgres testcontainer session fixtures (self-contained) ----------------


def _docker_running() -> bool:
    import shutil
    import subprocess

    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=True
        )
        return True
    except Exception:  # noqa: BLE001 — container availability check
        return False


@pytest.fixture(scope="session")
def postgres_url() -> str:
    if not _docker_running():
        pytest.skip("Docker daemon not available — skipping usage E2E tests")
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16-alpine", driver="psycopg")
    container.start()
    sync_url = container.get_connection_url()
    async_url = sync_url

    saved = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = ""
    try:
        from echo_v2.persistence.db import run_migrations

        run_migrations(sync_url)
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved
        else:
            os.environ.pop("DATABASE_URL", None)

    yield async_url
    container.stop()


@pytest_asyncio.fixture(scope="session")
async def engine(postgres_url: str):
    eng = create_async_engine(postgres_url, pool_pre_ping=True)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def clean_usage_db(engine):
    """Truncate ai_usage_events before each test for isolation."""
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "TRUNCATE TABLE ai_usage_events RESTART IDENTITY CASCADE"
        )
    yield


@pytest_asyncio.fixture
async def recorder(session_factory, clean_usage_db) -> AiUsageRecorder:
    """Build a real AiUsageRecorder backed by Postgres."""
    from echo_v2.persistence.postgres_ai_usage_events import (
        PostgresAiUsageEventRepository,
    )

    repo = PostgresAiUsageEventRepository(session_factory)
    return AiUsageRecorder(repo=repo, pricing=PricingService())


async def _dump_usage(session_factory, label: str) -> list[dict[str, object]]:
    """Query all ai_usage_events rows and print them. Returns the rows as dicts."""
    async with session_factory() as session:
        result = await session.execute(select(AiUsageEventRow))
        rows = result.scalars().all()
    print(f"\n{'=' * 70}")
    print(f"[usage] {label} — {len(rows)} event(s) recorded:")
    print(f"{'=' * 70}")
    for row in rows:
        print(
            f"  provider={row.provider:<7} operation={row.operation:<20} "
            f"model={row.model:<25} status={row.status:<7} "
            f"cost_source={row.cost_source:<22} "
            f"cost=${row.estimated_cost_usd or 0} "
            f"price_v={row.price_version}"
        )
        if row.prompt_tokens is not None or row.completion_tokens is not None:
            print(
                f"           tokens: prompt={row.prompt_tokens} "
                f"completion={row.completion_tokens} total={row.total_tokens}"
            )
        if row.usage_details:
            print(f"           details: {dict(row.usage_details)}")
        if row.error_type:
            print(f"           error_type: {row.error_type}")
    print()
    return [
        {
            "provider": r.provider,
            "operation": r.operation,
            "model": r.model,
            "status": r.status,
            "cost_source": r.cost_source,
            "estimated_cost_usd": r.estimated_cost_usd,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "total_tokens": r.total_tokens,
            "usage_details": r.usage_details,
        }
        for r in rows
    ]


def _real_openai_client():
    from openai import AsyncOpenAI

    return AsyncOpenAI()


# --- OpenAI image summarization ----------------------------------------------


@skip_no_openai
@skip_no_image
async def test_e2e_usage_openai_image(recorder, session_factory):
    """Real OpenAI vision call → usage event recorded with measured tokens."""
    from echo_v2.services.media_summarizer import summarize_image_bytes

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
            recorder=recorder,
            child_user_id=None,
        )
    finally:
        await client.close()

    print(f"[image] summary: {result.text[:80]}...")
    assert result.text, "Expected non-empty image summary"

    events = await _dump_usage(session_factory, "OpenAI image")
    assert len(events) == 1, f"Expected 1 usage event, got {len(events)}"
    ev = events[0]
    assert ev["provider"] == "openai"
    assert ev["operation"] == "media_image"
    assert ev["status"] == "success"
    assert ev["prompt_tokens"] is not None and ev["prompt_tokens"] > 0
    assert ev["completion_tokens"] is not None and ev["completion_tokens"] > 0
    assert ev["cost_source"] == "measured_usage_priced"
    assert ev["estimated_cost_usd"] is not None


# --- OpenAI PDF/document summarization ----------------------------------------


@skip_no_openai
@skip_no_pdf
async def test_e2e_usage_openai_pdf(recorder, session_factory):
    """Real OpenAI PDF call → usage event recorded with measured tokens."""
    from echo_v2.services.media_summarizer import summarize_document_bytes

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
            recorder=recorder,
            child_user_id=None,
        )
    finally:
        await client.close()

    print(f"[pdf] summary: {result.text[:80]}...")
    assert result.text, "Expected non-empty PDF summary"

    events = await _dump_usage(session_factory, "OpenAI PDF")
    assert len(events) >= 1, f"Expected >=1 usage event, got {len(events)}"
    # PDF may use vision (gpt-4o) and/or text (gpt-4.1) — at least one is recorded.
    for ev in events:
        assert ev["provider"] == "openai"
        assert ev["status"] == "success"
        assert ev["prompt_tokens"] is not None and ev["prompt_tokens"] > 0


# --- OpenAI link summarization ------------------------------------------------


@skip_no_openai
async def test_e2e_usage_openai_link(recorder, session_factory):
    """Real OpenAI link call → usage event recorded with measured tokens."""
    from echo_v2.services.media_summarizer import summarize_link

    client = _real_openai_client()
    try:
        result = await summarize_link(
            client=client,
            model="gpt-4.1",
            url="https://example.com",
            recorder=recorder,
            child_user_id=None,
        )
    finally:
        await client.close()

    print(f"[link] summary: {result.text[:80]}...")
    assert result.text, "Expected non-empty link summary"

    events = await _dump_usage(session_factory, "OpenAI link")
    assert len(events) == 1, f"Expected 1 usage event, got {len(events)}"
    ev = events[0]
    assert ev["provider"] == "openai"
    assert ev["operation"] == "media_link"
    assert ev["status"] == "success"
    assert ev["prompt_tokens"] is not None and ev["prompt_tokens"] > 0
    assert ev["cost_source"] == "measured_usage_priced"


# --- Modal audio transcription ------------------------------------------------


@skip_no_modal
@skip_no_audio
async def test_e2e_usage_modal_audio(recorder, session_factory):
    """Real Modal Whisper call → usage event recorded with audio duration + fallback cost."""
    import tempfile

    from echo_v2.integrations.modal.settings import load_settings
    from echo_v2.integrations.modal.transcriber import ModalWhisperTranscriber
    from echo_v2.services.transcription import ensure_transcribable_audio

    audio_bytes = _AUDIO_FILE.read_bytes()
    print(f"\n[audio] file={_AUDIO_FILE.name} size={len(audio_bytes)} bytes")

    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = Path(tmpdir) / _AUDIO_FILE.name
        raw_path.write_bytes(audio_bytes)
        transcribable_path = ensure_transcribable_audio(raw_path)
        converted_bytes = transcribable_path.read_bytes()

        settings = load_settings()
        transcriber = ModalWhisperTranscriber(settings=settings, recorder=recorder)
        result = await transcriber.transcribe_bytes(
            audio_bytes=converted_bytes,
            filename=transcribable_path.name,
            content_type="audio/ogg",
            child_user_id=None,
        )

    print(f"[audio] transcript: {result.text[:80]}...")
    assert result.text, "Expected non-empty transcript"

    events = await _dump_usage(session_factory, "Modal audio")
    assert len(events) == 1, f"Expected 1 usage event, got {len(events)}"
    ev = events[0]
    assert ev["provider"] == "modal"
    assert ev["operation"] == "media_audio"
    assert ev["status"] == "success"
    assert ev["cost_source"] == "fallback_estimate"
    assert ev["estimated_cost_usd"] is not None and ev["estimated_cost_usd"] > 0
    details = ev["usage_details"] or {}
    assert details.get("audio_duration_seconds") is not None
    assert details.get("audio_duration_seconds", 0) > 0
    print(
        f"[audio] duration={details.get('audio_duration_seconds')}s "
        f"cost=${ev['estimated_cost_usd']}"
    )


# --- Gemini video summarization ----------------------------------------------


@skip_no_gemini
@skip_no_video
async def test_e2e_usage_gemini_video(recorder, session_factory):
    """Real Gemini video call → usage event recorded with measured tokens."""
    from echo_v2.services.gemini_video_summarizer import GeminiVideoSummarizer

    video_bytes = _VIDEO_FILE.read_bytes()  # type: ignore[union-attr]
    print(f"\n[video] file={_VIDEO_FILE.name} size={len(video_bytes)} bytes")  # type: ignore[union-attr]

    summarizer = GeminiVideoSummarizer(
        api_key=os.environ["GEMINI_API_KEY"],
        model=os.environ.get("GEMINI_VIDEO_MODEL", "gemini-3.5-flash-lite"),
        recorder=recorder,
    )
    try:
        from unittest.mock import AsyncMock, patch

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
                child_user_id=None,
            )
    finally:
        await summarizer.close()

    print(f"[video] summary: {result.text[:80]}...")
    assert result.text, "Expected non-empty video summary"

    events = await _dump_usage(session_factory, "Gemini video")
    assert len(events) == 1, f"Expected 1 usage event, got {len(events)}"
    ev = events[0]
    assert ev["provider"] == "google"
    assert ev["operation"] == "media_video"
    assert ev["status"] == "success"
    # Gemini may or may not return token counts depending on the model.
    # If it does, cost_source should be measured_usage_priced.
    # If it doesn't, cost_source should be fallback_estimate or unknown.
    assert ev["cost_source"] in (
        "measured_usage_priced",
        "fallback_estimate",
        "unknown",
    )
    if ev["prompt_tokens"] is not None:
        assert ev["prompt_tokens"] > 0
        assert ev["cost_source"] == "measured_usage_priced"


# --- Gemini YouTube summarization ---------------------------------------------


@skip_no_gemini
async def test_e2e_usage_gemini_youtube(recorder, session_factory):
    """Real Gemini YouTube call → usage event recorded."""
    from echo_v2.services.gemini_video_summarizer import GeminiVideoSummarizer

    _YOUTUBE_URL = "https://www.youtube.com/watch?v=9hE5-98ZeCg"

    summarizer = GeminiVideoSummarizer(
        api_key=os.environ["GEMINI_API_KEY"],
        model=os.environ.get("GEMINI_VIDEO_MODEL", "gemini-3.5-flash-lite"),
        youtube_max_seconds=int(
            os.environ.get("GEMINI_VIDEO_YOUTUBE_MAX_SECONDS", "60")
        ),
        recorder=recorder,
    )
    try:
        result = await summarizer.summarize_youtube(
            _YOUTUBE_URL, child_user_id=None
        )
    finally:
        await summarizer.close()

    print(f"[youtube] summary: {result.text[:80]}...")
    assert result.text, "Expected non-empty YouTube summary"

    events = await _dump_usage(session_factory, "Gemini YouTube")
    assert len(events) == 1, f"Expected 1 usage event, got {len(events)}"
    ev = events[0]
    assert ev["provider"] == "google"
    assert ev["operation"] == "media_video"
    assert ev["status"] == "success"


# --- Guard analyzers (real OpenAI LLM) ----------------------------------------


@skip_no_openai
async def test_e2e_usage_guard_safety_analyzer(recorder, session_factory):
    """Real SimpleSafetyAnalyzer call → usage event recorded."""
    from echo_v2.domain.guard_simple import (
        SimpleConversation,
        SimpleConversationMessage,
    )
    from echo_v2.services.guard_simple_analyzer import SimpleSafetyAnalyzer

    now = datetime.now(timezone.utc)
    conversation = SimpleConversation(
        child_user_id=None,
        child_name="Shay",
        chat_id="chat-e2e-guard@g.us",
        chat_type="group",
        connection_id="conn-1",
        timezone="Asia/Jerusalem",
        window_start=now,
        window_end=now,
        monitored_child_role="child",
        messages=(
            SimpleConversationMessage("m1", "other", "They keep excluding Shay."),
            SimpleConversationMessage("m2", "child", "Please stop."),
        ),
    )

    client = _real_openai_client()
    try:
        analyzer = SimpleSafetyAnalyzer(
            client=client,
            model=os.environ.get("GUARD_LLM_MODEL_NAME", "gpt-4.1"),
            recorder=recorder,
        )
        result = await analyzer.analyze(conversation)
    finally:
        await client.close()

    print(f"[guard] parent_should_know={result.parent_should_know} summary={result.summary}")
    events = await _dump_usage(session_factory, "Guard safety analyzer")
    assert len(events) == 1, f"Expected 1 usage event, got {len(events)}"
    ev = events[0]
    assert ev["provider"] == "openai"
    assert ev["operation"] == "guard_security"
    assert ev["status"] == "success"
    assert ev["prompt_tokens"] is not None and ev["prompt_tokens"] > 0
    assert ev["cost_source"] == "measured_usage_priced"
    assert ev["estimated_cost_usd"] is not None and ev["estimated_cost_usd"] > 0


@skip_no_openai
async def test_e2e_usage_guard_exposure_analyzer(recorder, session_factory):
    """Real ExposureAnalyzer call → usage event recorded."""
    from echo_v2.domain.guard_simple import (
        SimpleConversation,
        SimpleConversationMessage,
    )
    from echo_v2.services.guard_exposure_analyzer import ExposureAnalyzer

    now = datetime.now(timezone.utc)
    conversation = SimpleConversation(
        child_user_id=None,
        child_name="Shay",
        chat_id="chat-e2e-guard@g.us",
        chat_type="group",
        connection_id="conn-1",
        timezone="Asia/Jerusalem",
        window_start=now,
        window_end=now,
        monitored_child_role="child",
        messages=(
            SimpleConversationMessage("m1", "other", "Nobody wants Shay here."),
            SimpleConversationMessage("m2", "other_2", "Shay is a loser."),
        ),
    )

    client = _real_openai_client()
    try:
        analyzer = ExposureAnalyzer(
            client=client,
            model=os.environ.get("GUARD_LLM_MODEL_NAME", "gpt-4.1"),
            recorder=recorder,
        )
        result = await analyzer.analyze(conversation)
    finally:
        await client.close()

    print(f"[exposure] significant={result.significant_exposure} summary={result.summary}")
    events = await _dump_usage(session_factory, "Guard exposure analyzer")
    assert len(events) == 1, f"Expected 1 usage event, got {len(events)}"
    ev = events[0]
    assert ev["provider"] == "openai"
    assert ev["operation"] == "guard_exposure"
    assert ev["status"] == "success"
    assert ev["prompt_tokens"] is not None and ev["prompt_tokens"] > 0
    assert ev["cost_source"] == "measured_usage_priced"
