"""Tests for shared, versioned lazy media enrichment."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from echo_v2.domain.chat import Message
from echo_v2.domain.media_enrichment import EnrichmentKind, EnrichmentStatus
from echo_v2.domain.pipeline_event import PipelineEvent
from echo_v2.persistence.chat_repositories import InMemoryMessageRepository
from echo_v2.persistence.media_enrichment import InMemoryMessageEnrichmentRepository
from echo_v2.ports.whatsapp import MessageDirection
from echo_v2.services.media_enrichment import MediaEnrichmentService
from echo_v2.services.media_summarizer import MediaSummary

pytestmark = pytest.mark.asyncio

class Resolver:
    async def resolve(self, message: Message) -> str:
        return "https://media.test/file"

class Summarizer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
    async def summarize_media(self, **kwargs: object) -> MediaSummary:
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider unavailable")
        return MediaSummary(text="a threatening image", kind="image", model="test-model")
    async def summarize_link(self, url: str, **kwargs: object) -> MediaSummary:
        self.calls += 1
        return MediaSummary(text="a useful page", kind="link", model="test-model")
    async def close(self) -> None:
        pass

def image(message_id: str = "message-1") -> Message:
    return Message(id=message_id, user_id="user", connection_id="connection", chat_id="chat@c.us",
                   provider_message_id=message_id, direction=MessageDirection.INBOUND, sender_id="sender",
                   timestamp=datetime.now(timezone.utc), message_type="image",
                   media_reference="baileys:c1:image-1", media_mime_type="image/jpeg",
                   media_file_name="image.jpg")

def service(repo: InMemoryMessageEnrichmentRepository, summarizer: Summarizer | None = None, **kwargs: object) -> MediaEnrichmentService:
    return MediaEnrichmentService(message_repo=InMemoryMessageRepository(), enrichment_repo=repo,
                                   media_summarizer=summarizer, media_url_resolver=Resolver(), **kwargs)

async def test_enrichment_is_persisted_and_reused() -> None:
    repo = InMemoryMessageEnrichmentRepository(); summarizer = Summarizer()
    first = (await service(repo, summarizer).enrich([image()]))[0]
    second = (await service(repo, summarizer).enrich([image()]))[0]
    assert first.effective_text == second.effective_text == "a threatening image"
    assert second.enrichment is not None and second.enrichment.status is EnrichmentStatus.COMPLETED
    assert summarizer.calls == 1

async def test_failed_enrichment_is_retryable_then_terminal() -> None:
    result = (await service(InMemoryMessageEnrichmentRepository(), Summarizer(fail=True),
                            max_attempts=1, retry_base_seconds=0).enrich([image()]))[0]
    assert result.enrichment is not None and result.enrichment.status is EnrichmentStatus.TERMINAL
    assert result.enrichment.error == "provider unavailable"

async def test_retryable_failure_can_be_reclaimed_and_completed() -> None:
    repo = InMemoryMessageEnrichmentRepository(); summarizer = Summarizer(fail=True)
    worker = service(repo, summarizer, max_attempts=3, retry_base_seconds=0)
    first = (await worker.enrich([image("retry")]))[0]
    assert first.enrichment is not None and first.enrichment.status is EnrichmentStatus.FAILED
    summarizer.fail = False
    second = (await worker.enrich([image("retry")]))[0]
    assert second.effective_text == "a threatening image" and summarizer.calls == 2

async def test_audio_enrichment_does_not_mutate_raw_message() -> None:
    message = Message(**{**image("audio").__dict__, "message_type": "audio",
                         "media_reference": None, "media_download_url": "https://media.test/a.ogg"})
    worker = MediaEnrichmentService(message_repo=InMemoryMessageRepository(),
                                    enrichment_repo=InMemoryMessageEnrichmentRepository(), transcriber=object())
    with patch("echo_v2.services.media_enrichment.handle_direct_audio_download_url",
               new=AsyncMock(return_value="spoken threat")):
        result = (await worker.enrich([message]))[0]
    assert result.effective_text == "spoken threat" and message.text is None

async def test_link_and_existing_text_paths() -> None:
    link = Message(id="link", user_id="u", connection_id="c", chat_id="chat@c.us", provider_message_id="link",
                   direction=MessageDirection.INBOUND, sender_id="s", timestamp=datetime.now(timezone.utc),
                   message_type="text", text="https://example.com")
    result = (await service(InMemoryMessageEnrichmentRepository(), Summarizer()).enrich([link]))[0]
    assert result.effective_text == "https://example.com — a useful page"
    caption = Message(**{**image("caption").__dict__, "text": "existing caption"})
    plain = (await service(InMemoryMessageEnrichmentRepository(), Summarizer()).enrich([caption]))[0]
    assert plain.effective_text == "existing caption" and plain.enrichment is None

async def test_in_memory_claim_lease_and_terminal_behaviors() -> None:
    repo = InMemoryMessageEnrichmentRepository(); now = datetime.now(timezone.utc)
    first = await repo.claim(message_id="m", enrichment_version="v", owner="a", lease_seconds=60, now=now)
    assert first is not None
    assert await repo.claim(message_id="m", enrichment_version="v", owner="b", lease_seconds=60, now=now) is None
    assert not await repo.complete(message_id="m", enrichment_version="v", owner="b", kind=EnrichmentKind.MEDIA_SUMMARY,
                                    text="x", model=None, now=now)
    assert await repo.fail(message_id="m", enrichment_version="v", owner="a", error="fatal", next_retry_at=None, terminal=True, now=now)
    assert await repo.claim(message_id="m", enrichment_version="v", owner="c", lease_seconds=60, now=now) is None

async def test_expired_lease_and_future_retry() -> None:
    repo = InMemoryMessageEnrichmentRepository(); now = datetime.now(timezone.utc)
    await repo.claim(message_id="expired", enrichment_version="v", owner="a", lease_seconds=0, now=now)
    reclaimed = await repo.claim(message_id="expired", enrichment_version="v", owner="b", lease_seconds=60, now=now)
    assert reclaimed is not None and reclaimed.lease_owner == "b"

async def test_missing_url_provider_and_empty_result_failures() -> None:
    missing = Message(**{**image("audio-missing").__dict__, "message_type": "audio", "media_reference": None, "media_download_url": "url"})
    result = (await MediaEnrichmentService(message_repo=InMemoryMessageRepository(), enrichment_repo=InMemoryMessageEnrichmentRepository()).enrich([missing]))[0]
    assert result.enrichment is not None and "transcriber" in (result.enrichment.error or "")
    empty = Summarizer(); empty.summarize_media = AsyncMock(return_value=MediaSummary(text="", kind="image", model="m"))
    result = (await service(InMemoryMessageEnrichmentRepository(), empty, retry_base_seconds=0).enrich([image("empty")]))[0]
    assert result.enrichment is not None and "empty text" in (result.enrichment.error or "")

async def test_properties_and_owner_completion() -> None:
    repo = InMemoryMessageEnrichmentRepository(); now = datetime.now(timezone.utc)
    claimed = await repo.claim(message_id="owner", enrichment_version="v", owner="a", lease_seconds=60, now=now)
    assert claimed is not None
    assert not await repo.fail(message_id="owner", enrichment_version="v", owner="b", error="x", next_retry_at=None, terminal=True, now=now)
    assert await repo.complete(message_id="owner", enrichment_version="v", owner="a", kind=EnrichmentKind.MEDIA_SUMMARY, text="done", model=None, now=now)
    result = (await service(InMemoryMessageEnrichmentRepository(), Summarizer()).enrich([image("properties")]))[0]
    assert result.usable_evidence and not result.incomplete

async def test_terminal_and_future_records_are_not_reprocessed() -> None:
    repo = InMemoryMessageEnrichmentRepository()
    now = datetime.now(timezone.utc)
    await repo.claim(message_id="terminal", enrichment_version="v", owner="a", lease_seconds=60, now=now)
    await repo.fail(message_id="terminal", enrichment_version="v", owner="a", error="fatal", next_retry_at=None, terminal=True, now=now)
    terminal = (await service(repo, Summarizer(), enrichment_version="v").enrich([image("terminal")]))[0]
    assert terminal.enrichment is not None and terminal.enrichment.status is EnrichmentStatus.TERMINAL
    await repo.claim(message_id="future", enrichment_version="v", owner="a", lease_seconds=60, now=now)
    retry_at = now + timedelta(hours=1)
    await repo.fail(message_id="future", enrichment_version="v", owner="a", error="wait", next_retry_at=retry_at, terminal=False, now=now)
    future = (await service(repo, Summarizer(), enrichment_version="v").enrich([image("future")]))[0]
    assert future.enrichment is not None and future.enrichment.next_retry_at == retry_at


async def test_resolver_without_url_is_recorded_as_failure() -> None:
    class EmptyResolver:
        async def resolve(self, message: Message) -> str | None:
            return None
    result = (await MediaEnrichmentService(
        message_repo=InMemoryMessageRepository(), enrichment_repo=InMemoryMessageEnrichmentRepository(),
        media_summarizer=Summarizer(), media_url_resolver=EmptyResolver(), retry_base_seconds=0,
    ).enrich([image("empty-url")]))[0]
    assert result.enrichment is not None and "download URL" in (result.enrichment.error or "")


async def test_service_returns_existing_claim_without_duplicate_provider_call() -> None:
    repo = InMemoryMessageEnrichmentRepository()
    now = datetime.now(timezone.utc)
    await repo.claim(message_id="claimed", enrichment_version="v", owner="other", lease_seconds=60, now=now)
    result = (await service(repo, Summarizer(), enrichment_version="v").enrich([image("claimed")]))[0]
    assert result.effective_text == ""
    assert result.enrichment is not None
    assert result.enrichment.lease_owner == "other"


async def test_resolver_returning_no_url_fails_cleanly() -> None:
    class EmptyResolver:
        async def resolve(self, message: Message) -> str | None:
            return None
    result = (await MediaEnrichmentService(
        message_repo=InMemoryMessageRepository(), enrichment_repo=InMemoryMessageEnrichmentRepository(),
        media_summarizer=Summarizer(), media_url_resolver=EmptyResolver(), retry_base_seconds=0,
    ).enrich([image("no-url")]))[0]
    assert result.enrichment is not None
    assert "download URL" in (result.enrichment.error or "")


async def test_lost_enrichment_lease_does_not_use_uncommitted_text() -> None:
    class LostCommitRepository(InMemoryMessageEnrichmentRepository):
        async def complete(self, **kwargs: object) -> bool:
            return False
    repo = LostCommitRepository()
    result = (await service(repo, Summarizer()).enrich([image("lost")]))[0]
    assert result.effective_text == ""
    assert result.enrichment is not None


async def test_media_reference_without_resolver_is_recorded() -> None:
    result = (await MediaEnrichmentService(
        message_repo=InMemoryMessageRepository(), enrichment_repo=InMemoryMessageEnrichmentRepository(),
        media_summarizer=Summarizer(), retry_base_seconds=0,
    ).enrich([image("no-resolver")]))[0]
    assert result.enrichment is not None
    assert "resolver" in (result.enrichment.error or "")


async def test_producer_validation() -> None:
    worker = MediaEnrichmentService(message_repo=InMemoryMessageRepository(), enrichment_repo=InMemoryMessageEnrichmentRepository())
    with pytest.raises(RuntimeError, match="transcriber"):
        await worker._produce(image(), EnrichmentKind.TRANSCRIPTION)
    with pytest.raises(RuntimeError, match="summarizer"):
        await worker._produce(image(), EnrichmentKind.MEDIA_SUMMARY)
    worker = service(InMemoryMessageEnrichmentRepository(), Summarizer())
    with pytest.raises(RuntimeError, match="no URL"):
        await worker._produce(image(), EnrichmentKind.LINK_SUMMARY)


# --- Video enrichment tests --------------------------------------------------


def video(message_id: str = "video-1", caption: str | None = None) -> Message:
    msg = Message(
        id=message_id, user_id="user", connection_id="connection", chat_id="chat@c.us",
        provider_message_id=message_id, direction=MessageDirection.INBOUND, sender_id="sender",
        timestamp=datetime.now(timezone.utc), message_type="video",
        media_reference="baileys:c1:video-1", media_mime_type="video/mp4",
        media_file_name="video.mp4",
    )
    if caption is not None:
        msg = Message(**{**msg.__dict__, "text": caption})
    return msg


class VideoSummarizer:
    """Summarizer that returns a video description, optionally with caption."""

    def __init__(self, *, fail_with: type[Exception] | None = None) -> None:
        self.fail_with = fail_with
        self.calls = 0

    async def summarize_media(self, **kwargs: object) -> MediaSummary:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with("video too large")
        caption = kwargs.get("caption")
        text = "ילד משחק בפארק עם כלב"
        if caption:
            text = f"{caption} | {text}"
        return MediaSummary(text=text, kind="video", model="gemini-3.5-flash-lite")

    async def summarize_link(self, url: str, **kwargs: object) -> MediaSummary:
        self.calls += 1
        return MediaSummary(text="a page", kind="link", model="test-model")

    async def close(self) -> None:
        pass


async def test_video_without_caption_is_enriched() -> None:
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = VideoSummarizer()
    result = (await service(repo, summarizer).enrich([video()]))[0]
    assert result.enrichment is not None
    assert result.enrichment.status is EnrichmentStatus.COMPLETED
    assert result.enrichment.kind is EnrichmentKind.MEDIA_SUMMARY
    assert "ילד" in result.effective_text


async def test_video_with_caption_is_enriched() -> None:
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = VideoSummarizer()
    result = (await service(repo, summarizer).enrich([video("vid-cap", caption="תראה")]))[0]
    assert result.enrichment is not None
    assert result.enrichment.status is EnrichmentStatus.COMPLETED
    # Caption should be preserved alongside the Gemini description.
    assert "תראה" in result.effective_text
    assert "ילד" in result.effective_text
    assert "|" in result.effective_text


async def test_video_too_large_is_terminal_immediately() -> None:
    from echo_v2.services.gemini_video_summarizer import VideoTooLargeError

    repo = InMemoryMessageEnrichmentRepository()
    summarizer = VideoSummarizer(fail_with=VideoTooLargeError)
    # max_attempts=3, but VideoTooLargeError should be terminal on the first attempt.
    result = (
        await service(
            repo, summarizer, max_attempts=3, retry_base_seconds=0,
        ).enrich([video("too-large")])
    )[0]
    assert result.enrichment is not None
    assert result.enrichment.status is EnrichmentStatus.TERMINAL
    assert "too large" in (result.enrichment.error or "")
    assert summarizer.calls == 1


async def test_video_transient_error_is_retryable() -> None:
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = VideoSummarizer(fail_with=RuntimeError)
    result = (
        await service(
            repo, summarizer, max_attempts=3, retry_base_seconds=0,
        ).enrich([video("transient")])
    )[0]
    assert result.enrichment is not None
    assert result.enrichment.status is EnrichmentStatus.FAILED
    assert summarizer.calls == 1


async def test_video_without_media_reference_or_url_is_terminal_immediately() -> None:
    """A video with no media_reference and no media_download_url is fundamentally
    unavailable — the connector did not provide a mediaKey. It should be marked
    terminal on the first attempt instead of burning 3 retries in ~75ms."""
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = VideoSummarizer()
    # Video with no media_reference and no media_download_url, but with a caption.
    msg = Message(
        id="no-ref", user_id="user", connection_id="connection", chat_id="chat@c.us",
        provider_message_id="no-ref", direction=MessageDirection.INBOUND, sender_id="sender",
        timestamp=datetime.now(timezone.utc), message_type="video",
        text="a caption", media_mime_type="video/mp4",
    )
    result = (
        await service(
            repo, summarizer, max_attempts=3, retry_base_seconds=0,
        ).enrich([msg])
    )[0]
    assert result.enrichment is not None
    assert result.enrichment.status is EnrichmentStatus.TERMINAL
    assert "unavailable" in (result.enrichment.error or "")
    # The summarizer should never be called — we fail before reaching _produce.
    assert summarizer.calls == 0
    # The caption text should still be available as effective_text.
    assert result.effective_text == "a caption"


async def test_video_uses_separate_enrichment_version() -> None:
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = VideoSummarizer()
    svc = MediaEnrichmentService(
        message_repo=InMemoryMessageRepository(),
        enrichment_repo=repo,
        media_summarizer=summarizer,
        media_url_resolver=Resolver(),
        enrichment_version="2026-10-17.1",
        video_enrichment_version="2026-10-23.video.1",
        retry_base_seconds=0,
    )
    result = (await svc.enrich([video("versioned")]))[0]
    assert result.enrichment is not None
    assert result.enrichment.enrichment_version == "2026-10-23.video.1"


async def test_image_uses_shared_version_when_video_version_set() -> None:
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = Summarizer()
    svc = MediaEnrichmentService(
        message_repo=InMemoryMessageRepository(),
        enrichment_repo=repo,
        media_summarizer=summarizer,
        media_url_resolver=Resolver(),
        enrichment_version="2026-10-17.1",
        video_enrichment_version="2026-10-23.video.1",
        retry_base_seconds=0,
    )
    result = (await svc.enrich([image("img-versioned")]))[0]
    assert result.enrichment is not None
    assert result.enrichment.enrichment_version == "2026-10-17.1"


# --- Signed URL refresh tests -----------------------------------------------


class CountingResolver:
    """Resolver that records every call and returns a fresh URL each time."""

    def __init__(self) -> None:
        self.calls: list[Message] = []

    async def resolve(self, message: Message) -> str:
        self.calls.append(message)
        return f"https://media.test/fresh/{len(self.calls)}"


def image_with_stale_url(message_id: str = "stale-1") -> Message:
    """An image message that already has a (potentially expired) download URL."""
    return Message(
        id=message_id, user_id="user", connection_id="connection", chat_id="chat@c.us",
        provider_message_id=message_id, direction=MessageDirection.INBOUND, sender_id="sender",
        timestamp=datetime.now(timezone.utc), message_type="image",
        media_reference="baileys:c1:image-1", media_mime_type="image/jpeg",
        media_file_name="image.jpg", media_download_url="https://media.test/expired",
    )


async def test_expired_media_url_is_refreshed_on_retry() -> None:
    """Even when a message has a pre-existing (expired) download URL, the
    resolver is called on every enrichment attempt so a fresh signed URL is
    used — expired URLs no longer prematurely exhaust enrichment retries."""
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = Summarizer(fail=True)
    resolver = CountingResolver()
    svc = MediaEnrichmentService(
        message_repo=InMemoryMessageRepository(), enrichment_repo=repo,
        media_summarizer=summarizer, media_url_resolver=resolver,
        max_attempts=3, retry_base_seconds=0,
    )
    first = (await svc.enrich([image_with_stale_url("stale-1")]))[0]
    assert first.enrichment is not None and first.enrichment.status is EnrichmentStatus.FAILED
    assert len(resolver.calls) == 1

    # Second attempt should also call the resolver (not reuse the stale URL).
    summarizer.fail = False
    second = (await svc.enrich([image_with_stale_url("stale-1")]))[0]
    assert second.effective_text == "a threatening image"
    assert len(resolver.calls) == 2


async def test_resolver_always_called_when_media_reference_present() -> None:
    """Regression guard: even when media_download_url is pre-set, the resolver
    is invoked — the guard was changed from 'no url' to 'media_reference present'."""
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = Summarizer()
    resolver = CountingResolver()
    svc = MediaEnrichmentService(
        message_repo=InMemoryMessageRepository(), enrichment_repo=repo,
        media_summarizer=summarizer, media_url_resolver=resolver, retry_base_seconds=0,
    )
    result = (await svc.enrich([image_with_stale_url("pre-set")]))[0]
    assert result.enrichment is not None and result.enrichment.status is EnrichmentStatus.COMPLETED
    assert len(resolver.calls) == 1


# --- Video link enrichment tests --------------------------------------------


class RecordingPipelineEventRepository:
    """PipelineEventRepository that records all events in memory."""

    def __init__(self) -> None:
        self.events: list[PipelineEvent] = []

    async def record(self, event: PipelineEvent) -> None:
        self.events.append(event)


class VideoLinkSummarizer:
    """Summarizer that supports summarize_media (direct video) and summarize_youtube."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.media_calls = 0
        self.youtube_calls = 0

    async def summarize_media(self, **kwargs: object) -> MediaSummary:
        self.media_calls += 1
        if self.fail:
            raise RuntimeError("provider unavailable")
        return MediaSummary(text="ילד משחק בפארק", kind="video", model="gemini-3.5-flash-lite")

    async def summarize_youtube(self, url: str) -> MediaSummary:
        self.youtube_calls += 1
        if self.fail:
            raise RuntimeError("provider unavailable")
        return MediaSummary(text="סרטון יוטיוב", kind="video", model="gemini-3.5-flash-lite")

    async def summarize_link(self, url: str, **kwargs: object) -> MediaSummary:
        return MediaSummary(text="a page", kind="link", model="test-model")

    async def close(self) -> None:
        pass


def mp4_link(message_id: str = "mp4-1") -> Message:
    return Message(
        id=message_id, user_id="user", connection_id="connection", chat_id="chat@c.us",
        provider_message_id=message_id, direction=MessageDirection.INBOUND, sender_id="sender",
        timestamp=datetime.now(timezone.utc), message_type="text",
        text="https://cdn.example.com/video.mp4",
    )


def youtube_link(message_id: str = "yt-1") -> Message:
    return Message(
        id=message_id, user_id="user", connection_id="connection", chat_id="chat@c.us",
        provider_message_id=message_id, direction=MessageDirection.INBOUND, sender_id="sender",
        timestamp=datetime.now(timezone.utc), message_type="text",
        text="https://www.youtube.com/watch?v=abc123",
    )


def vlink_service(
    repo: InMemoryMessageEnrichmentRepository,
    summarizer: VideoLinkSummarizer,
    *,
    pipeline_events: RecordingPipelineEventRepository | None = None,
    **kwargs: object,
) -> MediaEnrichmentService:
    return MediaEnrichmentService(
        message_repo=InMemoryMessageRepository(), enrichment_repo=repo,
        media_summarizer=summarizer, video_link_enabled=True,
        pipeline_events=pipeline_events, retry_base_seconds=0, **kwargs,
    )


async def test_mp4_link_routes_to_video_link_summary() -> None:
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = VideoLinkSummarizer()
    result = (await vlink_service(repo, summarizer).enrich([mp4_link()]))[0]
    assert result.enrichment is not None
    assert result.enrichment.status is EnrichmentStatus.COMPLETED
    assert result.enrichment.kind is EnrichmentKind.VIDEO_LINK_SUMMARY
    assert summarizer.media_calls == 1
    assert summarizer.youtube_calls == 0
    assert "video.mp4" in result.effective_text
    assert "ילד" in result.effective_text


async def test_youtube_link_routes_to_video_link_summary() -> None:
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = VideoLinkSummarizer()
    result = (await vlink_service(repo, summarizer).enrich([youtube_link()]))[0]
    assert result.enrichment is not None
    assert result.enrichment.status is EnrichmentStatus.COMPLETED
    assert result.enrichment.kind is EnrichmentKind.VIDEO_LINK_SUMMARY
    assert summarizer.media_calls == 0
    assert summarizer.youtube_calls == 1
    assert "youtube.com" in result.effective_text
    assert "יוטיוב" in result.effective_text


async def test_video_link_disabled_falls_through_to_link_summary() -> None:
    """When video_link_enabled=False, a video URL uses LINK_SUMMARY (existing behavior)."""
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = VideoLinkSummarizer()
    svc = MediaEnrichmentService(
        message_repo=InMemoryMessageRepository(), enrichment_repo=repo,
        media_summarizer=summarizer, video_link_enabled=False, retry_base_seconds=0,
    )
    result = (await svc.enrich([mp4_link()]))[0]
    assert result.enrichment is not None
    assert result.enrichment.kind is EnrichmentKind.LINK_SUMMARY
    # summarize_link is called, not summarize_media
    assert summarizer.media_calls == 0


async def test_video_link_uses_separate_enrichment_version() -> None:
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = VideoLinkSummarizer()
    svc = MediaEnrichmentService(
        message_repo=InMemoryMessageRepository(), enrichment_repo=repo,
        media_summarizer=summarizer, video_link_enabled=True,
        enrichment_version="2026-10-17.1",
        video_link_enrichment_version="2026-10-24.video_link.1",
        retry_base_seconds=0,
    )
    result = (await svc.enrich([youtube_link()]))[0]
    assert result.enrichment is not None
    assert result.enrichment.enrichment_version == "2026-10-24.video_link.1"


async def test_video_link_success_event_has_url_kind() -> None:
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = VideoLinkSummarizer()
    events = RecordingPipelineEventRepository()
    svc = vlink_service(repo, summarizer, pipeline_events=events)
    await svc.enrich([youtube_link()])
    completed = [e for e in events.events if e.event_name == "media.enrichment.completed"]
    assert len(completed) == 1
    assert completed[0].metadata.get("url_kind") == "youtube"


async def test_video_link_failure_event_has_url_kind() -> None:
    """url_kind is classified before _produce, so failure events carry it too."""
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = VideoLinkSummarizer(fail=True)
    events = RecordingPipelineEventRepository()
    svc = vlink_service(repo, summarizer, pipeline_events=events, max_attempts=1)
    await svc.enrich([youtube_link()])
    failed = [e for e in events.events if e.event_name == "media.enrichment.failed"]
    assert len(failed) == 1
    assert failed[0].metadata.get("url_kind") == "youtube"


async def test_video_link_mp4_failure_event_has_url_kind() -> None:
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = VideoLinkSummarizer(fail=True)
    events = RecordingPipelineEventRepository()
    svc = vlink_service(repo, summarizer, pipeline_events=events, max_attempts=1)
    await svc.enrich([mp4_link()])
    failed = [e for e in events.events if e.event_name == "media.enrichment.failed"]
    assert len(failed) == 1
    assert failed[0].metadata.get("url_kind") == "direct_video"


async def test_video_link_transient_failure_is_retryable() -> None:
    repo = InMemoryMessageEnrichmentRepository()
    summarizer = VideoLinkSummarizer(fail=True)
    result = (
        await vlink_service(repo, summarizer, max_attempts=3).enrich([youtube_link()])
    )[0]
    assert result.enrichment is not None
    assert result.enrichment.status is EnrichmentStatus.FAILED
    assert summarizer.youtube_calls == 1


async def test_video_link_previously_link_summary_reanalyzed_under_new_version() -> None:
    """A YouTube URL previously enriched as LINK_SUMMARY (shared version) is
    reanalyzed as VIDEO_LINK_SUMMARY under the new version — the old row is
    not consulted."""
    repo = InMemoryMessageEnrichmentRepository()
    # First, enrich as LINK_SUMMARY with video_link disabled (shared version).
    summarizer = VideoLinkSummarizer()
    svc_old = MediaEnrichmentService(
        message_repo=InMemoryMessageRepository(), enrichment_repo=repo,
        media_summarizer=summarizer, video_link_enabled=False,
        enrichment_version="2026-10-17.1", retry_base_seconds=0,
    )
    result_old = (await svc_old.enrich([youtube_link("yt-existing")]))[0]
    assert result_old.enrichment is not None
    assert result_old.enrichment.kind is EnrichmentKind.LINK_SUMMARY
    # Now enable video link with a distinct version — should reanalyze.
    svc_new = MediaEnrichmentService(
        message_repo=InMemoryMessageRepository(), enrichment_repo=repo,
        media_summarizer=summarizer, video_link_enabled=True,
        enrichment_version="2026-10-17.1",
        video_link_enrichment_version="2026-10-24.video_link.1",
        retry_base_seconds=0,
    )
    result_new = (await svc_new.enrich([youtube_link("yt-existing")]))[0]
    assert result_new.enrichment is not None
    assert result_new.enrichment.kind is EnrichmentKind.VIDEO_LINK_SUMMARY
    assert result_new.enrichment.enrichment_version == "2026-10-24.video_link.1"
    assert "יוטיוב" in result_new.effective_text
