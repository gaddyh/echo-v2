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

from sqlalchemy import desc, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from echo_v2.domain.chat import ChatState, Message
from echo_v2.domain.waiting_for_me import (
    WaitingForMeActive,
    WaitingForMeDecision,
    WaitingForMeResult,
)
from echo_v2.persistence.orm import (
    ChatRow,
    MessageRow,
    WaitingForMeActiveRow,
    WaitingForMeResultRow,
)
from echo_v2.ports.whatsapp import MessageDirection

__all__ = [
    "PostgresChatStateRepository",
    "PostgresMessageRepository",
    "PostgresWaitingForMeActiveRepository",
    "PostgresWaitingForMeResultRepository",
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
                    sender_name=message.sender_name,
                    chat_name=message.chat_name,
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

    async def list_for_analysis(
        self,
        *,
        user_id: str,
        chat_id: str,
        context_messages: int = 5,
        max_no_outbound: int = 20,
    ) -> list[Message]:
        """Load messages relevant for chat analysis.

        Uses ``ROW_NUMBER() OVER (ORDER BY timestamp, created_at, id)`` for
        deterministic ordering even when messages share a timestamp.

        Finds the last outbound message. Returns all messages after it
        (the inbound burst) plus ``context_messages`` messages before it
        for context. The last outbound is included in the window.

        If no outbound exists, returns the last ``max_no_outbound`` messages.
        """
        async with self._session() as session:
            # Build a CTE with row numbers for deterministic ordering.
            ordered = (
                select(
                    MessageRow.id,
                    MessageRow.direction,
                    func.row_number()
                    .over(
                        order_by=(
                            MessageRow.timestamp,
                            MessageRow.created_at,
                            MessageRow.id,
                        )
                    )
                    .label("rn"),
                )
                .where(
                    MessageRow.user_id == user_id,
                    MessageRow.chat_id == chat_id,
                )
                .cte("ordered")
            )

            # Find the row number of the last outbound message.
            last_outbound_rn = (
                select(ordered.c.rn)
                .where(ordered.c.direction == MessageDirection.OUTBOUND.value)
                .order_by(desc(ordered.c.rn))
                .limit(1)
            )
            outbound_rn = (
                await session.execute(last_outbound_rn)
            ).scalar_one_or_none()

            if outbound_rn is None:
                # No outbound — return last max_no_outbound messages.
                max_rn = (
                    select(func.max(ordered.c.rn)).select_from(ordered)
                )
                total = (await session.execute(max_rn)).scalar_one()
                if total is None:
                    return []
                start_rn = max(1, total - max_no_outbound + 1)
                stmt = (
                    select(MessageRow)
                    .join(ordered, MessageRow.id == ordered.c.id)
                    .where(ordered.c.rn >= start_rn)
                    .order_by(
                        MessageRow.timestamp,
                        MessageRow.created_at,
                        MessageRow.id,
                    )
                )
            else:
                # Include context_messages before the outbound + the outbound
                # + everything after it.
                start_rn = max(1, outbound_rn - context_messages)
                stmt = (
                    select(MessageRow)
                    .join(ordered, MessageRow.id == ordered.c.id)
                    .where(ordered.c.rn >= start_rn)
                    .order_by(
                        MessageRow.timestamp,
                        MessageRow.created_at,
                        MessageRow.id,
                    )
                )

            rows = (await session.execute(stmt)).scalars().all()
            return [self._row_to_domain(r) for r in rows]

    async def get_latest_inbound(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> Message | None:
        async with self._session() as session:
            stmt = (
                select(MessageRow)
                .where(
                    MessageRow.user_id == user_id,
                    MessageRow.chat_id == chat_id,
                    MessageRow.direction == MessageDirection.INBOUND.value,
                )
                .order_by(
                    desc(MessageRow.timestamp),
                    desc(MessageRow.created_at),
                    desc(MessageRow.id),
                )
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_domain(row)

    @staticmethod
    def _row_to_domain(row: MessageRow) -> Message:
        return Message(
            id=str(row.id),
            user_id=str(row.user_id),
            connection_id=str(row.connection_id),
            chat_id=row.chat_id,
            provider_message_id=row.provider_message_id,
            direction=MessageDirection(row.direction),
            sender_id=row.sender_id,
            sender_name=row.sender_name,
            chat_name=row.chat_name,
            timestamp=row.timestamp,
            message_type=row.message_type,
            text=row.text,
        )


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
        chat_name: str | None = None,
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
                    chat_name=chat_name,
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "chat_id"],
                    set_={
                        "activity_version": ChatRow.activity_version + 1,
                        "last_message_at": observed_at,
                        "last_direction": direction.value,
                        "next_analysis_at": next_analysis_at,
                        "updated_at": datetime.now(timezone.utc),
                        **({"chat_name": chat_name} if chat_name else {}),
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
            chat_name=row.chat_name,
        )


# --- PostgresWaitingForMeResultRepository -----------------------------------


class PostgresWaitingForMeResultRepository:
    """PostgreSQL implementation of :class:`WaitingForMeResultRepository`.

    Stores one immutable row per analysis run. ``save`` is a simple INSERT
    — no dedup, no upsert. If the worker reprocesses the same version
    (e.g. after a crash), a new row is created with a new ``id`` and
    ``created_at``.
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

    async def save(
        self,
        *,
        user_id: str,
        chat_id: str,
        result: WaitingForMeResult,
    ) -> str:
        async with self._session() as session:
            stmt = (
                pg_insert(WaitingForMeResultRow)
                .values(
                    user_id=user_id,
                    chat_id=chat_id,
                    target_version=result.target_version,
                    decision=result.decision.value,
                    confidence=result.confidence,
                    reason=result.reason,
                )
                .returning(WaitingForMeResultRow.id)
            )
            row_id = (await session.execute(stmt)).scalar_one()
            return str(row_id)

    async def list_recent(
        self,
        *,
        user_id: str,
        chat_id: str,
        limit: int = 10,
    ) -> list[WaitingForMeResult]:
        async with self._session() as session:
            stmt = (
                select(WaitingForMeResultRow)
                .where(
                    WaitingForMeResultRow.user_id == user_id,
                    WaitingForMeResultRow.chat_id == chat_id,
                )
                .order_by(desc(WaitingForMeResultRow.created_at))
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._row_to_domain(r) for r in rows]

    @staticmethod
    def _row_to_domain(row: WaitingForMeResultRow) -> WaitingForMeResult:
        return WaitingForMeResult(
            decision=WaitingForMeDecision(row.decision),
            confidence=row.confidence,
            reason=row.reason,
            target_version=row.target_version,
        )


# --- PostgresWaitingForMeActiveRepository -----------------------------------


class PostgresWaitingForMeActiveRepository:
    """PostgreSQL implementation of :class:`WaitingForMeActiveRepository`.

    Uses ``INSERT ... ON CONFLICT DO UPDATE`` to upsert the active row.
    On conflict, ``waiting_since`` and ``notified_at`` are preserved
    (not overwritten by the UPDATE).
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

    async def upsert(
        self,
        *,
        user_id: str,
        chat_id: str,
        target_version: int,
        result_id: str,
        waiting_since: datetime,
        notified_at: datetime | None = None,
    ) -> None:
        async with self._session() as session:
            stmt = (
                pg_insert(WaitingForMeActiveRow)
                .values(
                    user_id=user_id,
                    chat_id=chat_id,
                    target_version=target_version,
                    result_id=result_id,
                    waiting_since=waiting_since,
                    notified_at=notified_at,
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "chat_id"],
                    set_={
                        "target_version": target_version,
                        "result_id": result_id,
                        "updated_at": datetime.now(timezone.utc),
                    },
                )
            )
            await session.execute(stmt)

    async def delete(self, *, user_id: str, chat_id: str) -> bool:
        from sqlalchemy import delete as sa_delete

        async with self._session() as session:
            stmt = sa_delete(WaitingForMeActiveRow).where(
                WaitingForMeActiveRow.user_id == user_id,
                WaitingForMeActiveRow.chat_id == chat_id,
            )
            result = await session.execute(stmt)
            return result.rowcount > 0

    async def get(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> WaitingForMeActive | None:
        async with self._session() as session:
            stmt = select(WaitingForMeActiveRow).where(
                WaitingForMeActiveRow.user_id == user_id,
                WaitingForMeActiveRow.chat_id == chat_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_domain(row)

    async def list_active(
        self,
        *,
        user_id: str,
        current_versions: dict[str, int],
    ) -> list[WaitingForMeActive]:
        async with self._session() as session:
            stmt = select(WaitingForMeActiveRow).where(
                WaitingForMeActiveRow.user_id == user_id,
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [
                self._row_to_domain(r)
                for r in rows
                if current_versions.get(r.chat_id) == r.target_version
            ]

    async def list_all_for_user(self, *, user_id: str) -> list[WaitingForMeActive]:
        async with self._session() as session:
            stmt = select(WaitingForMeActiveRow).where(
                WaitingForMeActiveRow.user_id == user_id,
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._row_to_domain(r) for r in rows]

    async def acknowledge(
        self,
        *,
        user_id: str,
        chat_id: str,
        acknowledged_at: datetime,
    ) -> bool:
        from sqlalchemy import update as sa_update

        async with self._session() as session:
            stmt = (
                sa_update(WaitingForMeActiveRow)
                .where(
                    WaitingForMeActiveRow.user_id == user_id,
                    WaitingForMeActiveRow.chat_id == chat_id,
                )
                .values(
                    acknowledged_at=acknowledged_at,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            result = await session.execute(stmt)
            return result.rowcount > 0

    async def snooze(
        self,
        *,
        user_id: str,
        chat_id: str,
        snoozed_until: datetime,
    ) -> bool:
        from sqlalchemy import update as sa_update

        async with self._session() as session:
            stmt = (
                sa_update(WaitingForMeActiveRow)
                .where(
                    WaitingForMeActiveRow.user_id == user_id,
                    WaitingForMeActiveRow.chat_id == chat_id,
                )
                .values(
                    snoozed_until=snoozed_until,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            result = await session.execute(stmt)
            return result.rowcount > 0

    @staticmethod
    def _row_to_domain(row: WaitingForMeActiveRow) -> WaitingForMeActive:
        return WaitingForMeActive(
            user_id=str(row.user_id),
            chat_id=row.chat_id,
            target_version=row.target_version,
            result_id=str(row.result_id),
            waiting_since=row.waiting_since,
            notified_at=row.notified_at,
            acknowledged_at=row.acknowledged_at,
            snoozed_until=row.snoozed_until,
        )
