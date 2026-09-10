"""Postgres-backed chat ingestion repositories.

Satisfies :class:`echo_v2.persistence.chat_repositories.MessageRepository`
and :class:`echo_v2.persistence.chat_repositories.ChatStateRepository`
against PostgreSQL via SQLAlchemy 2 async.

Session handling mirrors :class:`PostgresScheduledActionRepository`:
``session=None`` (default) → standalone mode; ``session=<shared>`` → UoW
mode where the enclosing unit of work owns the transaction boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from echo_v2.domain.chat import ChatState, Message
from echo_v2.persistence.orm import ChatRow, MessageRow
from echo_v2.ports.whatsapp import MessageDirection

__all__ = [
    "PostgresChatStateRepository",
    "PostgresMessageRepository",
]


class _SessionContext:
    """Async context manager for session lifecycle.

    In standalone mode (owns=True), commits on clean exit and closes.
    In UoW mode (owns=False), does nothing — the UoW owns the transaction.
    """

    def __init__(self, session: AsyncSession, *, owns: bool) -> None:
        self._session = session
        self._owns = owns

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if self._owns:
                if exc_type is None:
                    await self._session.commit()
                else:
                    await self._session.rollback()
        finally:
            if self._owns:
                await self._session.close()


class PostgresMessageRepository:
    """PostgreSQL implementation of :class:`MessageRepository`.

    ``save()`` uses ``INSERT ... ON CONFLICT (connection_id,
    provider_message_id) DO NOTHING RETURNING id`` — returns ``True`` iff
    a row was inserted (first occurrence), ``False`` on duplicate.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        session: AsyncSession | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._shared_session = session

    def _session(self) -> _SessionContext:
        if self._shared_session is not None:
            return _SessionContext(self._shared_session, owns=False)
        return _SessionContext(self._session_factory(), owns=True)

    async def save(self, message: Message) -> bool:
        async with self._session() as session:
            stmt = (
                pg_insert(MessageRow)
                .values(
                    id=message.id,
                    user_id=message.user_id,
                    connection_id=message.connection_id,
                    chat_id=message.chat_id,
                    provider_message_id=message.provider_message_id,
                    direction=message.direction.value,
                    sender_id=message.sender_id,
                    timestamp=message.timestamp,
                    message_type=message.message_type,
                    text=message.text,
                )
                .on_conflict_do_nothing(
                    index_elements=["connection_id", "provider_message_id"],
                )
                .returning(MessageRow.id)
            )
            result = await session.execute(stmt)
            inserted = result.scalar_one_or_none()
            return inserted is not None


class PostgresChatStateRepository:
    """PostgreSQL implementation of :class:`ChatStateRepository`.

    ``upsert_on_message`` atomically increments ``activity_version`` and
    stores the ``next_analysis_at`` value computed by the service. The repo
    is a thin persistence layer — business logic (computing
    ``next_analysis_at``, filtering chats) lives in :class:`ChatIngestionService`.

    ``mark_processed`` is conditional on ``activity_version = target_version``
    — it only succeeds if the version hasn't changed during processing.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        session: AsyncSession | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._shared_session = session

    def _session(self) -> _SessionContext:
        if self._shared_session is not None:
            return _SessionContext(self._shared_session, owns=False)
        return _SessionContext(self._session_factory(), owns=True)

    async def get(self, user_id: str, chat_id: str) -> ChatState | None:
        async with self._session() as session:
            stmt = select(ChatRow).where(
                ChatRow.user_id == user_id,
                ChatRow.chat_id == chat_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._row_to_domain(row) if row else None

    async def upsert_on_message(
        self,
        *,
        user_id: str,
        chat_id: str,
        direction: MessageDirection,
        observed_at: datetime,
        next_analysis_at: datetime | None,
    ) -> ChatState:
        async with self._session() as session:
            stmt = (
                pg_insert(ChatRow)
                .values(
                    user_id=user_id,
                    chat_id=chat_id,
                    activity_version=1,
                    last_message_at=observed_at,
                    last_direction=direction.value,
                    next_analysis_at=next_analysis_at,
                    last_processed_version=0,
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "chat_id"],
                    set_={
                        "activity_version": ChatRow.activity_version + 1,
                        "last_message_at": observed_at,
                        "last_direction": direction.value,
                        "next_analysis_at": next_analysis_at,
                        "updated_at": datetime.now(timezone.utc),
                    },
                )
                .returning(ChatRow)
            )
            row = (await session.execute(stmt)).scalar_one()
            return self._row_to_domain(row)

    async def list_due(self, now: datetime, *, limit: int = 20) -> list[ChatState]:
        async with self._session() as session:
            stmt = (
                select(ChatRow)
                .where(
                    ChatRow.next_analysis_at.is_not(None),
                    ChatRow.next_analysis_at <= now,
                    ChatRow.last_direction == MessageDirection.INBOUND.value,
                    ChatRow.activity_version > ChatRow.last_processed_version,
                )
                .order_by(ChatRow.next_analysis_at)
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._row_to_domain(r) for r in rows]

    async def mark_processed(
        self,
        user_id: str,
        chat_id: str,
        target_version: int,
    ) -> bool:
        async with self._session() as session:
            stmt = (
                update(ChatRow)
                .where(
                    ChatRow.user_id == user_id,
                    ChatRow.chat_id == chat_id,
                    ChatRow.activity_version == target_version,
                )
                .values(
                    last_processed_version=target_version,
                    next_analysis_at=None,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            result = await session.execute(stmt)
            return result.rowcount > 0

    @staticmethod
    def _row_to_domain(row: ChatRow) -> ChatState:
        return ChatState(
            user_id=str(row.user_id),
            chat_id=row.chat_id,
            activity_version=row.activity_version,
            last_message_at=row.last_message_at,
            last_direction=MessageDirection(row.last_direction),
            next_analysis_at=row.next_analysis_at,
            last_processed_version=row.last_processed_version,
        )
