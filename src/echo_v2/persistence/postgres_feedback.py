"""PostgreSQL implementations of feedback, action, and mute repositories.

All three use ``INSERT ... ON CONFLICT DO NOTHING`` for idempotency on
``provider_message_id``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.feedback import (
    ChatMute,
    FeedbackVerdict,
    WaitingForMeAction,
    WaitingForMeActionType,
    WaitingForMeFeedback,
)
from echo_v2.persistence.orm import (
    ChatMuteRow,
    WaitingForMeActionRow,
    WaitingForMeFeedbackRow,
)

__all__ = [
    "PostgresChatMuteRepository",
    "PostgresWaitingForMeActionRepository",
    "PostgresWaitingForMeFeedbackRepository",
]


class _SessionContext:
    """Context manager for an AsyncSession."""

    def __init__(self, session: AsyncSession, *, owns: bool) -> None:
        self._session = session
        self._owns = owns

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc: object) -> None:
        if self._owns:
            await self._session.commit()
            await self._session.close()


# --- PostgresWaitingForMeFeedbackRepository ---------------------------------


class PostgresWaitingForMeFeedbackRepository:
    """PostgreSQL implementation of :class:`WaitingForMeFeedbackRepository`."""

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

    async def record(
        self,
        *,
        user_id: str,
        chat_id: str,
        verdict: FeedbackVerdict,
        result_id: str | None = None,
        target_version: int | None = None,
        conversation_snapshot: dict | None = None,
        provider_message_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> WaitingForMeFeedback | None:
        async with self._session() as session:
            stmt = (
                pg_insert(WaitingForMeFeedbackRow)
                .values(
                    user_id=user_id,
                    chat_id=chat_id,
                    result_id=result_id,
                    target_version=target_version,
                    verdict=verdict.value,
                    conversation_snapshot=conversation_snapshot,
                    provider_message_id=provider_message_id,
                    expires_at=expires_at,
                )
                .on_conflict_do_nothing()
                .returning(WaitingForMeFeedbackRow)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_domain(row)

    async def delete_expired(self, *, now: datetime) -> int:
        async with self._session() as session:
            stmt = sa_delete(WaitingForMeFeedbackRow).where(
                WaitingForMeFeedbackRow.expires_at.is_not(None),
                WaitingForMeFeedbackRow.expires_at <= now,
            )
            result = await session.execute(stmt)
            return result.rowcount

    @staticmethod
    def _row_to_domain(row: WaitingForMeFeedbackRow) -> WaitingForMeFeedback:
        return WaitingForMeFeedback(
            user_id=str(row.user_id),
            chat_id=row.chat_id,
            result_id=str(row.result_id) if row.result_id else None,
            target_version=row.target_version,
            verdict=FeedbackVerdict(row.verdict),
            conversation_snapshot=row.conversation_snapshot,
            provider_message_id=row.provider_message_id,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )


# --- PostgresWaitingForMeActionRepository -----------------------------------


class PostgresWaitingForMeActionRepository:
    """PostgreSQL implementation of :class:`WaitingForMeActionRepository`."""

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

    async def record(
        self,
        *,
        user_id: str,
        chat_id: str,
        action_type: WaitingForMeActionType,
        active_id: str | None = None,
        target_version: int | None = None,
        action_payload: dict | None = None,
        provider_message_id: str | None = None,
    ) -> WaitingForMeAction | None:
        async with self._session() as session:
            stmt = (
                pg_insert(WaitingForMeActionRow)
                .values(
                    user_id=user_id,
                    chat_id=chat_id,
                    active_id=active_id,
                    target_version=target_version,
                    action_type=action_type.value,
                    action_payload=action_payload,
                    provider_message_id=provider_message_id,
                )
                .on_conflict_do_nothing(
                    index_elements=["user_id", "provider_message_id"],
                )
                .returning(WaitingForMeActionRow)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_domain(row)

    @staticmethod
    def _row_to_domain(row: WaitingForMeActionRow) -> WaitingForMeAction:
        return WaitingForMeAction(
            user_id=str(row.user_id),
            chat_id=row.chat_id,
            active_id=row.active_id,
            target_version=row.target_version,
            action_type=WaitingForMeActionType(row.action_type),
            action_payload=row.action_payload,
            provider_message_id=row.provider_message_id,
            created_at=row.created_at,
        )

    async def list_by_session(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> list[WaitingForMeAction]:
        """List actions for a waiting-list session."""
        async with self._session() as session:
            stmt = select(WaitingForMeActionRow).where(
                WaitingForMeActionRow.user_id == user_id,
                WaitingForMeActionRow.action_payload["waiting_list_session_id"].astext
                == session_id,
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._row_to_domain(r) for r in rows]


# --- PostgresChatMuteRepository --------------------------------------------


class PostgresChatMuteRepository:
    """PostgreSQL implementation of :class:`ChatMuteRepository`."""

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

    async def is_muted(
        self,
        *,
        user_id: str,
        chat_id: str,
        now: datetime,
    ) -> bool:
        async with self._session() as session:
            stmt = select(ChatMuteRow).where(
                ChatMuteRow.user_id == user_id,
                ChatMuteRow.chat_id == chat_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return False
            if row.permanent:
                return True
            if row.muted_until is not None and row.muted_until > now:
                return True
            # Expired temporary mute — clean up.
            if row.muted_until is not None and row.muted_until <= now:
                await session.execute(
                    sa_delete(ChatMuteRow).where(
                        ChatMuteRow.user_id == user_id,
                        ChatMuteRow.chat_id == chat_id,
                    )
                )
                return False
            return False

    async def mute_temporary(
        self,
        *,
        user_id: str,
        chat_id: str,
        muted_until: datetime,
    ) -> None:
        async with self._session() as session:
            stmt = (
                pg_insert(ChatMuteRow)
                .values(
                    user_id=user_id,
                    chat_id=chat_id,
                    muted_until=muted_until,
                    permanent=False,
                    updated_at=datetime.now(timezone.utc),
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "chat_id"],
                    set_={
                        "muted_until": muted_until,
                        "permanent": False,
                        "updated_at": datetime.now(timezone.utc),
                    },
                )
            )
            await session.execute(stmt)

    async def mute_permanent(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> None:
        async with self._session() as session:
            stmt = (
                pg_insert(ChatMuteRow)
                .values(
                    user_id=user_id,
                    chat_id=chat_id,
                    muted_until=None,
                    permanent=True,
                    updated_at=datetime.now(timezone.utc),
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "chat_id"],
                    set_={
                        "muted_until": None,
                        "permanent": True,
                        "updated_at": datetime.now(timezone.utc),
                    },
                )
            )
            await session.execute(stmt)

    async def unmute(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> bool:
        async with self._session() as session:
            stmt = sa_delete(ChatMuteRow).where(
                ChatMuteRow.user_id == user_id,
                ChatMuteRow.chat_id == chat_id,
            )
            result = await session.execute(stmt)
            return result.rowcount > 0

    async def get(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> ChatMute | None:
        async with self._session() as session:
            stmt = select(ChatMuteRow).where(
                ChatMuteRow.user_id == user_id,
                ChatMuteRow.chat_id == chat_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_domain(row)

    @staticmethod
    def _row_to_domain(row: ChatMuteRow) -> ChatMute:
        return ChatMute(
            user_id=str(row.user_id),
            chat_id=row.chat_id,
            muted_until=row.muted_until,
            permanent=row.permanent,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
