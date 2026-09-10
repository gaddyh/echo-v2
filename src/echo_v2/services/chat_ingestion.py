"""ChatIngestionService — atomic message save + chat state + queue management.

Orchestrates the ingestion of a ``ProviderMessageEvent`` in one atomic
transaction:

1. ``INSERT message ON CONFLICT DO NOTHING`` — dedup by
   ``(connection_id, provider_message_id)``.
2. If new: ``upsert_on_message`` — increment ``activity_version``, set
   ``last_message_at``, ``last_direction``, ``next_analysis_at``.
3. If duplicate: no-op (return ``False``).

The service owns the business logic:

* Computing ``next_analysis_at`` from ``quiet_period`` + direction.
  Both inbound and outbound schedule analysis after the quiet period —
  direction alone does not determine resolution. The LLM decides whether
  the waiting state persists.
* Filtering private/group chats (``private_only`` flag, default
  ``True`` — Green ``chat_id`` suffix ``@c.us`` = private, ``@g.us`` =
  group).

The repos are thin persistence layers — they store what the service
computes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from echo_v2.domain.chat import Message
from echo_v2.persistence.chat_repositories import (
    ChatStateRepository,
    MessageRepository,
)
from echo_v2.ports.whatsapp import ProviderMessageEvent

__all__ = ["ChatIngestionService"]

_logger = logging.getLogger("echo_v2.services.chat_ingestion")


class ChatIngestionService:
    """Atomic message ingestion + chat state + queue management.

    Args:
        message_repo: Stores immutable message records with dedup.
        chat_state_repo: Manages per-chat state (also the queue).
        quiet_period_seconds: How long after the last inbound message
            before the chat is eligible for analysis. Default 5 minutes.
        private_only: If ``True`` (default), skip group chats
            (``chat_id`` ending in ``@g.us``). Set to ``False`` to ingest
            all chats.
    """

    def __init__(
        self,
        message_repo: MessageRepository,
        chat_state_repo: ChatStateRepository,
        *,
        quiet_period_seconds: float = 300.0,
        private_only: bool = True,
    ) -> None:
        self._message_repo = message_repo
        self._chat_state_repo = chat_state_repo
        self._quiet_period = quiet_period_seconds
        self._private_only = private_only

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
        # Skip group chats if configured for private only.
        if self._private_only and not event.chat_id.endswith("@c.us"):
            return False

        now = datetime.now(timezone.utc)
        # Both inbound and outbound schedule analysis after the quiet
        # period. Direction alone does not determine resolution — the
        # LLM decides whether the waiting state persists.
        next_analysis_at = now + timedelta(seconds=self._quiet_period)

        message = Message(
            id=str(uuid.uuid4()),
            user_id=user_id,
            connection_id=connection_id,
            chat_id=event.chat_id,
            provider_message_id=event.provider_message_id,
            direction=event.direction,
            sender_id=None,
            timestamp=event.timestamp,
            message_type=event.kind.value,
            text=event.text,
        )

        inserted = await self._message_repo.save(message)
        if not inserted:
            return False  # duplicate, no-op

        await self._chat_state_repo.upsert_on_message(
            user_id=user_id,
            chat_id=event.chat_id,
            direction=event.direction,
            observed_at=now,
            next_analysis_at=next_analysis_at,
        )
        return True
