"""DailyDigest domain type — one digest per user per local_date.

The digest lifecycle:
  processing → sent (success)
  processing → empty (no active waiting chats)
  processing → indeterminate (send result unknown — no blind retry)
  processing → failed (permanent error)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

__all__ = ["DailyDigest", "DailyDigestStatus"]


class DailyDigestStatus(str, Enum):
    """Status of a daily digest."""

    PROCESSING = "processing"
    SENT = "sent"
    EMPTY = "empty"
    INDETERMINATE = "indeterminate"
    FAILED = "failed"


@dataclass(frozen=True)
class DailyDigest:
    """A single daily digest record.

    Attributes:
        id: Unique row ID.
        user_id: The user this digest belongs to.
        local_date: The date in the user's timezone (not UTC).
        status: The current :class:`DailyDigestStatus`.
        created_at: When the record was created.
        sent_at: When the digest was successfully sent (``None`` if not sent).
        provider_message_id: The message ID from the bot provider (``None``
            if not sent or send result unknown).
        item_count: Number of waiting chats included in the digest.
    """

    id: str
    user_id: str
    local_date: date
    status: DailyDigestStatus
    created_at: datetime
    sent_at: datetime | None = None
    provider_message_id: str | None = None
    item_count: int = 0
