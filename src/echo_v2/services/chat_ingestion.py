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

* Computing ``next_analysis_at`` from the provider message timestamp plus
  ``quiet_period``. Both inbound and outbound schedule analysis after the
  quiet period — direction alone does not determine resolution. The LLM
  decides whether the waiting state persists.
* Filtering private/group chats (``private_only`` flag, default
  ``True`` — Green ``chat_id`` suffix ``@c.us`` = private, ``@g.us`` =
  group).

The repo is a thin persistence layer — it stores what the service
computes, atomically.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from echo_v2.persistence.chat_repositories import AnalysisTarget, IngestionRepository
from echo_v2.persistence.guard_repositories import (
    GuardChatStateRepository,
    GuardianChildLinkRepository,
)
from echo_v2.ports.whatsapp import ProviderMessageEvent
from echo_v2.services.guard_schedule_policy import (
    GuardConversationState,
    GuardSchedulePolicy,
    GuardSchedulingContext,
)
from echo_v2.services.guard_taxonomy import GuardDecision

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
        child_link_repo: GuardianChildLinkRepository | None = None,
        guard_chat_state_repo: GuardChatStateRepository | None = None,
        guard_schedule_policy: GuardSchedulePolicy | None = None,
        guard_shadow_enabled: bool = False,
    ) -> None:
        self._ingestion_repo = ingestion_repo
        self._quiet_period = quiet_period_seconds
        self._private_only = private_only
        self._excluded_chat_ids = excluded_chat_ids
        self._child_links = child_link_repo
        self._guard_state = guard_chat_state_repo
        self._guard_policy = guard_schedule_policy or GuardSchedulePolicy()
        self._guard_shadow_enabled = guard_shadow_enabled

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

        # Prefer provider-supplied classification. The suffix fallback keeps
        # older Green events compatible without rewriting provider chat IDs.
        is_group = event.is_group
        if is_group is None:
            is_group = event.chat_id.endswith("@g.us")

        guard_enabled = False
        previous_guard_state = None
        if self._guard_shadow_enabled and self._child_links is not None:
            link = await self._child_links.get_active_for_child(user_id)
            guard_enabled = link is not None
            if guard_enabled and self._guard_state is not None:
                previous_guard_state = await self._guard_state.get(user_id, event.chat_id)

        if self._private_only and is_group and not guard_enabled:
            return False

        message_time = event.timestamp
        analysis_target = AnalysisTarget.GUARD if guard_enabled else AnalysisTarget.WFM
        if guard_enabled:
            previous_decision = (
                previous_guard_state.last_decision
                if previous_guard_state is not None
                else GuardDecision.NONE
            )
            schedule = self._guard_policy.on_message(
                GuardSchedulingContext(
                    state=GuardConversationState(decision=previous_decision),
                    is_group=is_group,
                    pending_since=(
                        previous_guard_state.pending_since
                        if previous_guard_state is not None
                        else None
                    ),
                    last_analysis_at=(
                        previous_guard_state.last_analysis_at
                        if previous_guard_state is not None
                        else None
                    ),
                    next_analysis_at=(
                        previous_guard_state.next_analysis_at
                        if previous_guard_state is not None
                        else None
                    ),
                    new_message_at=message_time,
                )
            )
            next_analysis_at = schedule.analyze_at
            guard_schedule_reason = schedule.reason
        else:
            guard_schedule_reason = None
            # WFM retains its provider-time quiet-period semantics.
            next_analysis_at = message_time + timedelta(seconds=self._quiet_period)

        from echo_v2.domain.chat import Message

        message = Message(
            id=str(uuid.uuid4()),
            user_id=user_id,
            connection_id=connection_id,
            chat_id=event.chat_id,
            provider_message_id=event.provider_message_id,
            direction=event.direction,
            sender_id=event.sender_id,
            source=event.source,
            sender_name=event.sender_name,
            chat_name=event.chat_name,
            timestamp=event.timestamp,
            message_type=event.kind.value,
            text=event.text,
            media_reference=event.media_reference,
            media_download_url=event.media_download_url,
            media_mime_type=event.media_mime_type,
            media_file_name=event.media_file_name,
        )

        return await self._ingestion_repo.ingest_if_new(
            message=message,
            direction=event.direction,
            observed_at=message_time,
            next_analysis_at=next_analysis_at,
            chat_name=event.chat_name,
            analysis_target=analysis_target,
            guard_schedule_reason=guard_schedule_reason,
        )
