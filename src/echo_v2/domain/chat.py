"""Chat ingestion domain models.

Two domain objects back the chat ingestion + analysis queue:

* :class:`Message` — an immutable record of a single WhatsApp message
  (inbound or outbound) stored once and deduplicated by
  ``(connection_id, provider_message_id)``.

* :class:`ChatState` — the compact per-chat state that doubles as the
  analysis queue. ``activity_version`` increments on every new message;
  ``next_analysis_at`` is set to ``now + quiet_period`` on inbound and
  ``NULL`` on outbound (cancelling any pending analysis). The worker
  polls chats where ``next_analysis_at`` has passed, processes them,
  and commits the result only if ``activity_version`` is unchanged
  (conditional ``mark_processed``).

These are **not** the full ConversationService models (roadmap §9.1,
Step 2). They are the minimal state needed for the ingestion + queue
mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from echo_v2.ports.whatsapp import MessageDirection

__all__ = [
    "ChatState",
    "Message",
]


class ChatAnalysisStatus(Enum):
    """Lifecycle state of a chat analysis job (for future use)."""

    PENDING = "pending"
    PROCESSING = "processing"


@dataclass
class Message:
    """An immutable record of a single WhatsApp message.

    Deduplicated by ``(connection_id, provider_message_id)`` — a
    duplicate webhook delivery of the same message is a no-op.

    ``sender_id`` is ``None`` for now; the Green adapter does not yet
    extract the actual sender in group chats. A follow-up will add
    ``sender_id`` to :class:`ProviderMessageEvent` for group support.
    """

    id: str
    user_id: str
    connection_id: str
    chat_id: str
    provider_message_id: str
    direction: MessageDirection
    sender_id: str | None
    sender_name: str | None = None
    chat_name: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    message_type: str = "text"
    text: str | None = None


@dataclass
class ChatState:
    """Compact per-chat state that also serves as the analysis queue.

    ``activity_version`` increments on every new message. ``next_analysis_at``
    is set to ``now + quiet_period`` on inbound messages and ``None`` on
    outbound (cancelling pending analysis). The worker polls for chats
    where ``next_analysis_at <= now()`` and ``activity_version >
    last_processed_version``.

    ``last_message_at`` uses ingestion time (``now()``), not the
    provider-reported timestamp — out-of-order webhook handling is
    deferred until we observe it in practice.
    """

    user_id: str
    chat_id: str
    activity_version: int
    last_message_at: datetime
    last_direction: MessageDirection
    next_analysis_at: datetime | None
    last_processed_version: int
    chat_name: str | None = None
