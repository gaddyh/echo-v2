"""Structured, versioned enrichment results for persisted messages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class EnrichmentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    TERMINAL = "terminal"


class EnrichmentKind(str, Enum):
    TRANSCRIPTION = "transcription"
    MEDIA_SUMMARY = "media_summary"
    LINK_SUMMARY = "link_summary"


@dataclass(frozen=True)
class MessageEnrichment:
    message_id: str
    enrichment_version: str
    status: EnrichmentStatus
    kind: EnrichmentKind | None = None
    text: str | None = None
    model: str | None = None
    error: str | None = None
    attempts: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    next_retry_at: datetime | None = None
    enriched_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_incomplete(self) -> bool:
        return self.status is not EnrichmentStatus.COMPLETED

    @property
    def is_retryable(self) -> bool:
        return self.status in {
            EnrichmentStatus.PENDING,
            EnrichmentStatus.PROCESSING,
            EnrichmentStatus.FAILED,
        }


__all__ = ["EnrichmentKind", "EnrichmentStatus", "MessageEnrichment"]
