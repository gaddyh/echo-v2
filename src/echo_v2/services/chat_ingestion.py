"""ChatIngestionService — atomic message save + chat state + queue management.

Orchestrates the ingestion of a ``ProviderMessageEvent`` in one atomic
transaction via :class:`IngestionRepository.ingest_if_new`:

1. ``INSERT message ON CONFLICT DO NOTHING`` — dedup by
   ``(connection_id, provider_message_id)``.
2. If new: ``upsert_on_message`` — increment ``activity_version``, set
   ``last_message_at``, ``last_direction``, ``next_analysis_at``.
3. If duplicate: no-op (return ``False``).

Steps 1 and 2 run in a single database transaction, so a crash between
them can never leave a message without a version bump.

The service owns the business logic:

* Computing ``next_analysis_at`` from ``quiet_period`` + direction.
  Both inbound and outbound schedule analysis after the quiet period —
  direction alone does not determine resolution. The LLM decides whether
  the waiting state persists.
* Filtering private/group chats (``private_only`` flag, default
  ``True`` — Green ``chat_id`` suffix ``@c.us`` = private, ``@g.us`` =
  group).

The repo is a thin persistence layer — it stores what the service
computes, atomically.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from echo_v2.persistence.chat_repositories import IngestionRepository
from echo_v2.ports.whatsapp import ProviderMessageEvent

__all__ = ["ChatIngestionService"]

_logger = logging.getLogger("echo_v2.services.chat_ingestion")


class ChatIngestionService:
    """Atomic message ingestion + chat state + queue management.

    Args:
        ingestion_repo: The :class:`IngestionRepository` that performs
            the message insert + chat state upsert in one transaction.
        quiet_period_seconds: How long after the last inbound message
            before the chat is eligible for analysis. Default 5 minutes.
        private_only: If ``True`` (default), skip group chats
            (``chat_id`` ending in ``@g.us``). Set to ``False`` to ingest
            all chats.
        excluded_chat_ids: Chat IDs to never ingest or schedule for
            analysis (e.g. the Echo bot's own service chat, whose
            reminders would otherwise be classified as the user's
            obligations). Defaults to empty.
    """

    def __init__(
        self,
        ingestion_repo: IngestionRepository,
        *,
        quiet_period_seconds: float = 300.0,
        private_only: bool = True,
        excluded_chat_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._ingestion_repo = ingestion_repo
        self._quiet_period = quiet_period_seconds
        self._private_only = private_only
        self._excluded_chat_ids = excluded_chat_ids

    async def ingest_message(
        self,
        event: ProviderMessageEvent,
        *,
        user_id: str,
        connection_id: str,
    ) -> bool:
        """Ingest a message event atomically.

        Returns ``True`` if a new message was stored, ``False`` if it was
        a duplicate or skipped (e.g. group chat with ``private_only``).
        """
        # Skip excluded service chats (e.g. the Echo bot's own chat) before
        # any scheduling — prevents self-referential analysis loops.
        if event.chat_id in self._excluded_chat_ids:
            return False

        # Skip group chats if configured for private only.
        if self._private_only and not event.chat_id.endswith("@c.us"):
            return False

        now = datetime.now(timezone.utc)
        # Both inbound and outbound schedule analysis after the quiet
        # period. Direction alone does not determine resolution — the
        # LLM decides whether the waiting state persists.
        next_analysis_at = now + timedelta(seconds=self._quiet_period)

        from echo_v2.domain.chat import Message

        message = Message(
            id=str(uuid.uuid4()),
            user_id=user_id,
            connection_id=connection_id,
            chat_id=event.chat_id,
            provider_message_id=event.provider_message_id,
            direction=event.direction,
            sender_id=event.sender_id,
            sender_name=event.sender_name,
            chat_name=event.chat_name,
            timestamp=event.timestamp,
            message_type=event.kind.value,
            text=event.text,
            audio_download_url=event.audio_download_url,
            audio_mime_type=event.audio_mime_type,
            audio_file_name=event.audio_file_name,
        )

        return await self._ingestion_repo.ingest_if_new(
            message=message,
            direction=event.direction,
            observed_at=now,
            next_analysis_at=next_analysis_at,
            chat_name=event.chat_name,
        )
