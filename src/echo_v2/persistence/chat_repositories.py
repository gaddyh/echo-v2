"""Chat ingestion repository protocols and in-memory implementations.

Two repositories back the chat ingestion + analysis queue:

* :class:`MessageRepository` — stores immutable message records,
  deduplicated by ``(connection_id, provider_message_id)``.

* :class:`ChatStateRepository` — manages per-chat state that doubles
  as the analysis queue. ``upsert_on_message`` atomically increments
  ``activity_version`` and stores the ``next_analysis_at`` value
  computed by the service. ``list_due`` returns chats ready for
  processing. ``mark_processed`` is conditional on
  ``activity_version`` — it only succeeds if the version hasn't
  changed during processing.

The service owns the business logic (computing ``next_analysis_at``
from ``quiet_period`` + direction, filtering private/group chats).
The repos are thin persistence layers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from echo_v2.domain.chat import ChatState, Message
from echo_v2.domain.waiting_for_me import WaitingForMeResult
from echo_v2.ports.whatsapp import MessageDirection

__all__ = [
    "ChatStateRepository",
    "InMemoryChatStateRepository",
    "InMemoryMessageRepository",
    "InMemoryWaitingForMeResultRepository",
    "MessageRepository",
    "WaitingForMeResultRepository",
]


# --- MessageRepository -----------------------------------------------------


@runtime_checkable
class MessageRepository(Protocol):
    """Store immutable message records with dedup."""

    async def save(self, message: Message) -> bool:
        """Insert a message. Returns ``True`` if inserted, ``False`` if duplicate."""
        ...

    async def list_for_analysis(
        self,
        *,
        user_id: str,
        chat_id: str,
        context_messages: int = 5,
        max_no_outbound: int = 20,
    ) -> list[Message]:
        """Load messages relevant for chat analysis.

        Finds the last outbound message in the chat. Returns all messages
        after it (the inbound burst that triggered analysis) plus
        ``context_messages`` messages before it for context.

        If no outbound message exists, returns the last ``max_no_outbound``
        messages.

        Ordered by timestamp ascending.
        """
        ...


class InMemoryMessageRepository:
    """Process-local message repository backed by a dict.

    Deduplicates on ``(connection_id, provider_message_id)``.
    """

    def __init__(self) -> None:
        self._messages: dict[tuple[str, str], Message] = {}

    async def save(self, message: Message) -> bool:
        key = (message.connection_id, message.provider_message_id)
        if key in self._messages:
            return False
        self._messages[key] = message
        return True

    async def list_for_analysis(
        self,
        *,
        user_id: str,
        chat_id: str,
        context_messages: int = 5,
        max_no_outbound: int = 20,
    ) -> list[Message]:
        from echo_v2.ports.whatsapp import MessageDirection

        # Filter to this chat, ordered by (timestamp, id) for determinism.
        chat_msgs = sorted(
            (m for m in self._messages.values()
             if m.user_id == user_id and m.chat_id == chat_id),
            key=lambda m: (m.timestamp, m.id),
        )
        if not chat_msgs:
            return []

        # Find the last outbound message.
        last_outbound_idx: int | None = None
        for i in range(len(chat_msgs) - 1, -1, -1):
            if chat_msgs[i].direction == MessageDirection.OUTBOUND:
                last_outbound_idx = i
                break

        if last_outbound_idx is None:
            # No outbound — return last max_no_outbound messages.
            return chat_msgs[-max_no_outbound:]

        # context_messages before the outbound + the outbound + everything after.
        start = max(0, last_outbound_idx - context_messages)
        return chat_msgs[start:]


# --- ChatStateRepository ---------------------------------------------------


@runtime_checkable
class ChatStateRepository(Protocol):
    """Manage per-chat state that doubles as the analysis queue."""

    async def get(self, user_id: str, chat_id: str) -> ChatState | None:
        """Return the chat state, or ``None`` if not found."""
        ...

    async def upsert_on_message(
        self,
        *,
        user_id: str,
        chat_id: str,
        direction: MessageDirection,
        observed_at: datetime,
        next_analysis_at: datetime | None,
    ) -> ChatState:
        """Atomically increment ``activity_version`` and update chat state.

        Sets ``last_message_at = observed_at``, ``last_direction = direction``,
        ``next_analysis_at = next_analysis_at``. Creates the row if it
        doesn't exist (``activity_version = 1``). Returns the post-upsert state.
        """
        ...

    async def list_due(self, now: datetime, *, limit: int = 20) -> list[ChatState]:
        """Return chats ready for analysis.

        Chats where ``next_analysis_at IS NOT NULL AND next_analysis_at <= now``
        and ``last_direction = 'inbound'`` and
        ``activity_version > last_processed_version``, ordered by
        ``next_analysis_at``, limited to ``limit``.
        """
        ...

    async def mark_processed(
        self,
        user_id: str,
        chat_id: str,
        target_version: int,
    ) -> bool:
        """Conditionally mark a chat as processed.

        Only succeeds if ``activity_version = target_version`` (the version
        hasn't changed during processing). Sets
        ``last_processed_version = target_version`` and
        ``next_analysis_at = NULL``. Returns ``True`` if updated.
        """
        ...


class InMemoryChatStateRepository:
    """Process-local chat state repository backed by a dict."""

    def __init__(self) -> None:
        self._chats: dict[tuple[str, str], ChatState] = {}

    async def get(self, user_id: str, chat_id: str) -> ChatState | None:
        return self._chats.get((user_id, chat_id))

    async def upsert_on_message(
        self,
        *,
        user_id: str,
        chat_id: str,
        direction: MessageDirection,
        observed_at: datetime,
        next_analysis_at: datetime | None,
    ) -> ChatState:
        key = (user_id, chat_id)
        existing = self._chats.get(key)
        if existing is None:
            chat = ChatState(
                user_id=user_id,
                chat_id=chat_id,
                activity_version=1,
                last_message_at=observed_at,
                last_direction=direction,
                next_analysis_at=next_analysis_at,
                last_processed_version=0,
            )
        else:
            chat = ChatState(
                user_id=user_id,
                chat_id=chat_id,
                activity_version=existing.activity_version + 1,
                last_message_at=observed_at,
                last_direction=direction,
                next_analysis_at=next_analysis_at,
                last_processed_version=existing.last_processed_version,
            )
        self._chats[key] = chat
        return chat

    async def list_due(self, now: datetime, *, limit: int = 20) -> list[ChatState]:
        due = [
            chat
            for chat in self._chats.values()
            if chat.next_analysis_at is not None
            and chat.next_analysis_at <= now
            and chat.last_direction == MessageDirection.INBOUND
            and chat.activity_version > chat.last_processed_version
        ]
        due.sort(key=lambda c: c.next_analysis_at)  # type: ignore[arg-type]
        return due[:limit]

    async def mark_processed(
        self,
        user_id: str,
        chat_id: str,
        target_version: int,
    ) -> bool:
        key = (user_id, chat_id)
        existing = self._chats.get(key)
        if existing is None:
            return False
        if existing.activity_version != target_version:
            return False
        self._chats[key] = ChatState(
            user_id=existing.user_id,
            chat_id=existing.chat_id,
            activity_version=existing.activity_version,
            last_message_at=existing.last_message_at,
            last_direction=existing.last_direction,
            next_analysis_at=None,
            last_processed_version=target_version,
        )
        return True


# Structural checks: in-memory impls satisfy the protocols.
_msg_repo: MessageRepository = InMemoryMessageRepository()  # type: ignore[assignment]
_chat_repo: ChatStateRepository = InMemoryChatStateRepository()  # type: ignore[assignment]


# --- WaitingForMeResultRepository -------------------------------------------


@runtime_checkable
class WaitingForMeResultRepository(Protocol):
    """Store immutable WaitingForMe analysis results."""

    async def save(
        self,
        *,
        user_id: str,
        chat_id: str,
        result: WaitingForMeResult,
    ) -> None:
        """Insert a result row. One row per analysis run."""
        ...

    async def list_recent(
        self,
        *,
        user_id: str,
        chat_id: str,
        limit: int = 10,
    ) -> list[WaitingForMeResult]:
        """Return recent results for a chat, newest first."""
        ...


class InMemoryWaitingForMeResultRepository:
    """Process-local result repository backed by a list."""

    def __init__(self) -> None:
        self._results: list[tuple[str, str, WaitingForMeResult]] = []

    async def save(
        self,
        *,
        user_id: str,
        chat_id: str,
        result: WaitingForMeResult,
    ) -> None:
        self._results.append((user_id, chat_id, result))

    async def list_recent(
        self,
        *,
        user_id: str,
        chat_id: str,
        limit: int = 10,
    ) -> list[WaitingForMeResult]:
        matching = [
            r for (uid, cid, r) in self._results
            if uid == user_id and cid == chat_id
        ]
        return list(reversed(matching))[:limit]


_wfm_repo: WaitingForMeResultRepository = InMemoryWaitingForMeResultRepository()  # type: ignore[assignment]
