"""Shared lazy enrichment of persisted media and link messages."""

from __future__ import annotations

import dataclasses
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from echo_v2.domain.chat import Message
from echo_v2.domain.media_enrichment import (
    EnrichmentKind,
    EnrichmentStatus,
    MessageEnrichment,
)
from echo_v2.domain.pipeline_event import PipelineEvent
from echo_v2.persistence.chat_repositories import MessageRepository
from echo_v2.persistence.media_enrichment import MessageEnrichmentRepository
from echo_v2.persistence.pipeline_events import PipelineEventRepository
from echo_v2.services.gemini_video_summarizer import VideoTooLargeError
from echo_v2.services.media_summarizer import (
    MediaSummarizer,
    extract_urls,
    is_link_message,
)
from echo_v2.services.transcription import Transcriber, handle_direct_audio_download_url

_logger = logging.getLogger("echo_v2.services.media_enrichment")

MEDIA_TYPES = frozenset({"image", "video", "document"})
DEFAULT_ENRICHMENT_VERSION = "2026-10-17.1"


@dataclass(frozen=True)
class EnrichedMessage:
    message: Message
    enrichment: MessageEnrichment | None = None
    effective_text: str = ""

    @property
    def usable_evidence(self) -> bool:
        return bool(self.effective_text.strip())

    @property
    def incomplete(self) -> bool:
        return self.enrichment is not None and self.enrichment.is_incomplete


class MediaEnrichmentService:
    def __init__(
        self,
        *,
        message_repo: MessageRepository,
        enrichment_repo: MessageEnrichmentRepository,
        transcriber: Transcriber | None = None,
        media_summarizer: MediaSummarizer | None = None,
        media_url_resolver: object | None = None,
        enrichment_version: str = DEFAULT_ENRICHMENT_VERSION,
        lease_seconds: int = 300,
        max_attempts: int = 3,
        retry_base_seconds: int = 60,
        pipeline_events: PipelineEventRepository | None = None,
        video_enrichment_version: str | None = None,
    ) -> None:
        self._messages = message_repo
        self._enrichments = enrichment_repo
        self._transcriber = transcriber
        self._summarizer = media_summarizer
        self._resolver = media_url_resolver
        self._version = enrichment_version
        self._video_version = video_enrichment_version
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._retry_base = retry_base_seconds
        self._pipeline_events = pipeline_events

    async def _record_event(self, event: PipelineEvent) -> None:
        if self._pipeline_events is None:
            return
        try:
            await self._pipeline_events.record(event)
        except Exception:
            _logger.exception("pipeline event persistence failed event_name=%s", event.event_name)

    def _version_for(self, message: Message) -> str:
        """Return the enrichment version for this message.

        Video messages use a separate ``video_enrichment_version`` (when
        configured) so only videos are re-analyzed on a version bump,
        leaving existing image/document/audio/link enrichments untouched.
        """
        if message.message_type == "video" and self._video_version is not None:
            return self._video_version
        return self._version

    async def enrich(self, messages: list[Message]) -> list[EnrichedMessage]:
        _logger.info(
            "media enrichment batch start count=%d version=%s message_ids=%s",
            len(messages), self._version, [message.id for message in messages],
        )
        results = [await self._enrich_message(message) for message in messages]
        _logger.info(
            "media enrichment batch complete count=%d statuses=%s",
            len(results), [
                result.enrichment.status.value if result.enrichment is not None else "not_required"
                for result in results
            ],
        )
        return results

    async def _enrich_message(self, message: Message) -> EnrichedMessage:
        started = time.monotonic()
        target = self._target(message)
        version = self._version_for(message)
        _logger.info(
            "media enrichment start message_id=%s type=%s target=%s has_text=%s has_url=%s has_reference=%s",
            message.id, message.message_type, target.value if target else "none",
            bool(message.text), bool(message.media_download_url), bool(message.media_reference),
        )
        if target is None:
            _logger.info("media enrichment skip message_id=%s reason=not_required", message.id)
            return EnrichedMessage(message=message, effective_text=message.text or "")

        current = await self._enrichments.get(
            message_id=message.id, enrichment_version=version
        )
        if current is not None:
            _logger.info(
                "media enrichment existing message_id=%s status=%s attempts=%d next_retry_at=%s",
                message.id, current.status.value, current.attempts, current.next_retry_at,
            )
        if current is not None and current.status is EnrichmentStatus.COMPLETED:
            _logger.info("media enrichment cache hit message_id=%s", message.id)
            return EnrichedMessage(message, current, current.text or message.text or "")
        if current is not None and current.status is EnrichmentStatus.TERMINAL:
            _logger.warning("media enrichment terminal message_id=%s error=%s", message.id, current.error)
            return EnrichedMessage(message, current, message.text or "")
        now = datetime.now(timezone.utc)
        if current is not None and current.next_retry_at is not None and current.next_retry_at > now:
            _logger.info("media enrichment defer message_id=%s until=%s", message.id, current.next_retry_at)
            return EnrichedMessage(message, current, message.text or "")

        owner = str(uuid.uuid4())
        _logger.info("media enrichment claiming message_id=%s owner=%s", message.id, owner)
        claimed = await self._enrichments.claim(
            message_id=message.id,
            enrichment_version=version,
            owner=owner,
            lease_seconds=self._lease_seconds,
            now=now,
        )
        if claimed is None or claimed.lease_owner != owner:
            _logger.info(
                "media enrichment claim lost message_id=%s owner=%s claimed_owner=%s",
                message.id, owner, claimed.lease_owner if claimed else None,
            )
            current = await self._enrichments.get(
                message_id=message.id, enrichment_version=version
            )
            return EnrichedMessage(message, current, message.text or "")

        try:
            _logger.info(
                "media enrichment claimed message_id=%s attempt=%d lease_expires_at=%s",
                message.id, claimed.attempts, claimed.lease_expires_at,
            )
            working = message
            if working.media_reference and target is not EnrichmentKind.LINK_SUMMARY:
                _logger.info("media enrichment resolving url message_id=%s", message.id)
                if self._resolver is None:
                    raise RuntimeError("media URL resolver is not configured")
                url = await self._resolver.resolve(working)  # type: ignore[attr-defined]
                if url:
                    working = dataclasses.replace(working, media_download_url=url)
                    _logger.info("media enrichment url resolved message_id=%s", message.id)
            if target is not EnrichmentKind.LINK_SUMMARY and not working.media_download_url:
                raise RuntimeError("media download URL is unavailable")
            _logger.info("media enrichment provider start message_id=%s kind=%s", message.id, target.value)
            kind, text, model = await self._produce(working, target)
            _logger.info(
                "media enrichment provider complete message_id=%s kind=%s model=%s output_chars=%d",
                message.id, kind.value, model, len(text),
            )
            if not text.strip():
                raise RuntimeError("enrichment provider returned empty text")
            completed = await self._enrichments.complete(
                message_id=message.id, enrichment_version=version,
                owner=owner, kind=kind, text=text, model=model, now=datetime.now(timezone.utc),
            )
            _logger.info("media enrichment persisted message_id=%s kind=%s", message.id, kind.value)
            if not completed:
                _logger.warning("media enrichment completion lost message_id=%s", message.id)
                current = await self._enrichments.get(
                    message_id=message.id, enrichment_version=version
                )
                return EnrichedMessage(message, current, message.text or "")
            result = await self._enrichments.get(
                message_id=message.id, enrichment_version=version
            )
            duration_ms = (time.monotonic() - started) * 1000
            _logger.info(
                "media enrichment success message_id=%s status=%s duration_ms=%.1f",
                message.id, result.status.value if result else None, duration_ms,
            )
            await self._record_event(PipelineEvent(
                event_name="media.enrichment.completed", pipeline="media_enrichment", status="completed",
                message_id=message.id, attempt=claimed.attempts, duration_ms=duration_ms,
                metadata={"kind": kind.value, "model": model},
            ))
            return EnrichedMessage(message, result, text)
        except Exception as exc:  # noqa: BLE001 - one bad media item must not abort a window
            attempts = claimed.attempts
            # Deterministic size-limit failures are terminal immediately —
            # retrying won't help and wastes attempts.
            terminal = isinstance(exc, VideoTooLargeError) or attempts >= self._max_attempts
            retry_at = None if terminal else datetime.now(timezone.utc) + timedelta(
                seconds=self._retry_base * (2 ** max(0, attempts - 1))
            )
            await self._enrichments.fail(
                message_id=message.id, enrichment_version=version,
                owner=owner, error=str(exc), next_retry_at=retry_at,
                terminal=terminal, now=datetime.now(timezone.utc),
            )
            _logger.warning(
                "media enrichment %s message_id=%s attempts=%d retry_at=%s duration_ms=%.1f error=%s",
                "terminally_failed" if terminal else "failed",
                message.id,
                attempts,
                retry_at,
                (time.monotonic() - started) * 1000,
                exc,
            )
            result = await self._enrichments.get(
                message_id=message.id, enrichment_version=version
            )
            await self._record_event(PipelineEvent(
                event_name="media.enrichment.failed", pipeline="media_enrichment",
                status="terminal" if terminal else "retryable", message_id=message.id,
                attempt=attempts, duration_ms=(time.monotonic() - started) * 1000,
                error_type=type(exc).__name__, error_message=str(exc),
                metadata={"retry_at": retry_at.isoformat() if retry_at else None},
            ))
            return EnrichedMessage(message, result, message.text or "")

    @staticmethod
    def _target(message: Message) -> EnrichmentKind | None:
        if message.message_type == "audio" and not message.text:
            return EnrichmentKind.TRANSCRIPTION
        if message.message_type == "video":
            return EnrichmentKind.MEDIA_SUMMARY
        if message.message_type in MEDIA_TYPES and not message.text:
            return EnrichmentKind.MEDIA_SUMMARY
        if message.message_type == "text" and is_link_message(message.text):
            return EnrichmentKind.LINK_SUMMARY
        return None

    async def _produce(self, message: Message, kind: EnrichmentKind) -> tuple[EnrichmentKind, str, str | None]:
        if kind is EnrichmentKind.TRANSCRIPTION:
            if self._transcriber is None:
                raise RuntimeError("transcriber is not configured")
            text = await handle_direct_audio_download_url(
                transcriber=self._transcriber,
                download_url=message.media_download_url or "",
                file_name=message.media_file_name or "voice-message",
                mime_type=message.media_mime_type or "",
            )
            return kind, text, getattr(self._transcriber, "model", None)
        if self._summarizer is None:
            raise RuntimeError("media summarizer is not configured")
        if kind is EnrichmentKind.MEDIA_SUMMARY:
            summary = await self._summarizer.summarize_media(
                download_url=message.media_download_url or "",
                mime_type=message.media_mime_type,
                file_name=message.media_file_name,
                message_type=message.message_type,
                caption=message.text,
            )
            # Preserve the original caption alongside the Gemini description
            # for videos — never replace one with the other.
            if message.message_type == "video" and message.text and summary.text:
                return kind, f"{message.text} | {summary.text}", summary.model
            return kind, summary.text, summary.model
        urls = extract_urls(message.text or "")
        if not urls:
            raise RuntimeError("link message contains no URL")
        summary = await self._summarizer.summarize_link(urls[0])
        text = f"{urls[0]} — {summary.text}" if summary.text else urls[0]
        return kind, text, summary.model


__all__ = ["DEFAULT_ENRICHMENT_VERSION", "EnrichedMessage", "MediaEnrichmentService"]
