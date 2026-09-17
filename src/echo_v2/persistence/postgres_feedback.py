"""PostgreSQL implementations of feedback, action, and mute repositories.

All three use ``INSERT ... ON CONFLICT DO NOTHING`` for idempotency on
``provider_message_id``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import CursorResult, select
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.feedback import (
    ActionCommandResult,
    ChatMute,
    ChatNotInterestedClicks,
    FeedbackVerdict,
    HandlingOutcome,
    WaitingForMeAction,
    WaitingForMeActionType,
    WaitingForMeFeedback,
)
from echo_v2.persistence.orm import (
    ChatMuteRow,
    ChatNotInterestedClickRow,
    WaitingForMeActionRow,
    WaitingForMeActiveRow,
    WaitingForMeFeedbackRow,
)

__all__ = [
    "PostgresChatMuteRepository",
    "PostgresChatNotInterestedClickRepository",
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
        conversation_snapshot: dict[str, Any] | None = None,
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
            result = cast(CursorResult[Any], await session.execute(stmt))
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
        action_payload: dict[str, Any] | None = None,
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

    # --- Atomic command methods (record + mutate in one transaction) ---

    async def _record_action(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        chat_id: str,
        action_type: WaitingForMeActionType,
        active_id: str | None = None,
        target_version: int | None = None,
        action_payload: dict[str, Any] | None = None,
        provider_message_id: str | None = None,
    ) -> WaitingForMeActionRow | None:
        """Insert action row. Returns None if duplicate (same provider_message_id)."""
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
        return result.scalar_one_or_none()

    async def resolve_and_delete_by_version(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        provider_message_id: str,
        action_payload: dict[str, Any] | None = None,
    ) -> ActionCommandResult:
        async with self._session_factory() as session:
            try:
                row = await self._record_action(
                    session,
                    user_id=user_id,
                    chat_id="",
                    action_type=WaitingForMeActionType.RESOLVE,
                    active_id=active_id,
                    target_version=target_version,
                    action_payload=action_payload,
                    provider_message_id=provider_message_id,
                )
                if row is None:
                    await session.rollback()
                    return ActionCommandResult(outcome=HandlingOutcome.DUPLICATE)

                del_stmt = (
                    sa_delete(WaitingForMeActiveRow)
                    .where(
                        WaitingForMeActiveRow.id == active_id,
                        WaitingForMeActiveRow.user_id == user_id,
                        WaitingForMeActiveRow.target_version == target_version,
                    )
                    .returning(
                        WaitingForMeActiveRow.chat_id,
                        WaitingForMeActiveRow.result_id,
                    )
                )
                result = await session.execute(del_stmt)
                deleted = result.one_or_none()
                if deleted is not None:
                    await session.commit()
                    return ActionCommandResult(
                        outcome=HandlingOutcome.APPLIED,
                        chat_id=deleted.chat_id,
                        result_id=str(deleted.result_id),
                    )

                # Not deleted — check if active exists.
                check = await session.execute(
                    select(WaitingForMeActiveRow).where(
                        WaitingForMeActiveRow.id == active_id,
                    )
                )
                exists = check.scalar_one_or_none()
                await session.rollback()
                if exists is None:
                    return ActionCommandResult(outcome=HandlingOutcome.NOT_FOUND)
                return ActionCommandResult(outcome=HandlingOutcome.STALE)
            except Exception:
                await session.rollback()
                raise

    async def resolve_and_delete_by_chat(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        provider_message_id: str,
        action_payload: dict[str, Any] | None = None,
    ) -> ActionCommandResult:
        async with self._session_factory() as session:
            try:
                row = await self._record_action(
                    session,
                    user_id=user_id,
                    chat_id="",
                    action_type=WaitingForMeActionType.RESOLVE,
                    active_id=active_id,
                    target_version=target_version,
                    action_payload=action_payload,
                    provider_message_id=provider_message_id,
                )
                if row is None:
                    await session.rollback()
                    return ActionCommandResult(outcome=HandlingOutcome.DUPLICATE)

                # Fetch active to verify ownership + version.
                sel_stmt = select(WaitingForMeActiveRow).where(
                    WaitingForMeActiveRow.id == active_id,
                )
                active = (
                    await session.execute(sel_stmt)
                ).scalar_one_or_none()
                if active is None:
                    await session.rollback()
                    return ActionCommandResult(outcome=HandlingOutcome.NOT_FOUND)
                if str(active.user_id) != user_id or active.target_version != target_version:
                    await session.rollback()
                    return ActionCommandResult(outcome=HandlingOutcome.STALE)

                # Delete by (user_id, chat_id).
                del_stmt = sa_delete(WaitingForMeActiveRow).where(
                    WaitingForMeActiveRow.user_id == user_id,
                    WaitingForMeActiveRow.chat_id == active.chat_id,
                )
                await session.execute(del_stmt)
                await session.commit()
                return ActionCommandResult(
                    outcome=HandlingOutcome.APPLIED,
                    chat_id=active.chat_id,
                    result_id=str(active.result_id),
                )
            except Exception:
                await session.rollback()
                raise

    async def snooze_active(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        snoozed_until: datetime,
        provider_message_id: str,
        action_payload: dict[str, Any] | None = None,
    ) -> ActionCommandResult:
        async with self._session_factory() as session:
            try:
                row = await self._record_action(
                    session,
                    user_id=user_id,
                    chat_id="",
                    action_type=WaitingForMeActionType.SNOOZE,
                    active_id=active_id,
                    target_version=target_version,
                    action_payload=action_payload,
                    provider_message_id=provider_message_id,
                )
                if row is None:
                    await session.rollback()
                    return ActionCommandResult(outcome=HandlingOutcome.DUPLICATE)

                upd_stmt = (
                    sa_update(WaitingForMeActiveRow)
                    .where(
                        WaitingForMeActiveRow.id == active_id,
                        WaitingForMeActiveRow.user_id == user_id,
                        WaitingForMeActiveRow.target_version == target_version,
                    )
                    .values(
                        snoozed_until=snoozed_until,
                        updated_at=datetime.now(timezone.utc),
                    )
                    .returning(WaitingForMeActiveRow.chat_id)
                )
                result = await session.execute(upd_stmt)
                updated = result.one_or_none()
                if updated is not None:
                    await session.commit()
                    return ActionCommandResult(
                        outcome=HandlingOutcome.APPLIED,
                        chat_id=updated.chat_id,
                    )

                # Not updated — check if active exists.
                check = await session.execute(
                    select(WaitingForMeActiveRow).where(
                        WaitingForMeActiveRow.id == active_id,
                    )
                )
                exists = check.scalar_one_or_none()
                await session.rollback()
                if exists is None:
                    return ActionCommandResult(outcome=HandlingOutcome.NOT_FOUND)
                return ActionCommandResult(outcome=HandlingOutcome.STALE)
            except Exception:
                await session.rollback()
                raise

    async def mute_chat_atomic(
        self,
        *,
        user_id: str,
        chat_id: str,
        permanent: bool,
        muted_until: datetime | None,
        provider_message_id: str,
    ) -> ActionCommandResult:
        async with self._session_factory() as session:
            try:
                row = await self._record_action(
                    session,
                    user_id=user_id,
                    chat_id=chat_id,
                    action_type=WaitingForMeActionType.MUTE_CHAT,
                    provider_message_id=provider_message_id,
                )
                if row is None:
                    await session.rollback()
                    return ActionCommandResult(outcome=HandlingOutcome.DUPLICATE)

                now = datetime.now(timezone.utc)
                if permanent:
                    mute_stmt = (
                        pg_insert(ChatMuteRow)
                        .values(
                            user_id=user_id,
                            chat_id=chat_id,
                            muted_until=None,
                            permanent=True,
                        )
                        .on_conflict_do_update(
                            index_elements=["user_id", "chat_id"],
                            set_={
                                "muted_until": None,
                                "permanent": True,
                                "updated_at": now,
                            },
                        )
                    )
                else:
                    assert muted_until is not None
                    mute_stmt = (
                        pg_insert(ChatMuteRow)
                        .values(
                            user_id=user_id,
                            chat_id=chat_id,
                            muted_until=muted_until,
                            permanent=False,
                        )
                        .on_conflict_do_update(
                            index_elements=["user_id", "chat_id"],
                            set_={
                                "muted_until": muted_until,
                                "permanent": False,
                                "updated_at": now,
                            },
                        )
                    )
                await session.execute(mute_stmt)
                await session.commit()
                return ActionCommandResult(outcome=HandlingOutcome.APPLIED)
            except Exception:
                await session.rollback()
                raise

    async def unmute_chat_atomic(
        self,
        *,
        user_id: str,
        chat_id: str,
        provider_message_id: str,
    ) -> ActionCommandResult:
        async with self._session_factory() as session:
            try:
                row = await self._record_action(
                    session,
                    user_id=user_id,
                    chat_id=chat_id,
                    action_type=WaitingForMeActionType.UNMUTE_CHAT,
                    provider_message_id=provider_message_id,
                )
                if row is None:
                    await session.rollback()
                    return ActionCommandResult(outcome=HandlingOutcome.DUPLICATE)

                del_stmt = sa_delete(ChatMuteRow).where(
                    ChatMuteRow.user_id == user_id,
                    ChatMuteRow.chat_id == chat_id,
                )
                await session.execute(del_stmt)
                await session.commit()
                return ActionCommandResult(outcome=HandlingOutcome.APPLIED)
            except Exception:
                await session.rollback()
                raise


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
            result = cast(CursorResult[Any], await session.execute(stmt))
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


# --- PostgresChatNotInterestedClickRepository --------------------------------


class PostgresChatNotInterestedClickRepository:
    """PostgreSQL implementation of :class:`ChatNotInterestedClickRepository`."""

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

    async def increment(
        self,
        *,
        user_id: str,
        chat_id: str,
        now: datetime,
    ) -> int:
        async with self._session() as session:
            stmt = (
                pg_insert(ChatNotInterestedClickRow)
                .values(
                    user_id=user_id,
                    chat_id=chat_id,
                    click_count=1,
                    last_click_at=now,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "chat_id"],
                    set_={
                        "click_count": ChatNotInterestedClickRow.click_count + 1,
                        "last_click_at": now,
                        "updated_at": now,
                    },
                )
                .returning(ChatNotInterestedClickRow.click_count)
            )
            result = await session.execute(stmt)
            return int(result.scalar_one())

    async def reset(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> None:
        async with self._session() as session:
            stmt = sa_delete(ChatNotInterestedClickRow).where(
                ChatNotInterestedClickRow.user_id == user_id,
                ChatNotInterestedClickRow.chat_id == chat_id,
            )
            await session.execute(stmt)

    async def get(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> ChatNotInterestedClicks | None:
        async with self._session() as session:
            stmt = select(ChatNotInterestedClickRow).where(
                ChatNotInterestedClickRow.user_id == user_id,
                ChatNotInterestedClickRow.chat_id == chat_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_domain(row)

    @staticmethod
    def _row_to_domain(row: ChatNotInterestedClickRow) -> ChatNotInterestedClicks:
        return ChatNotInterestedClicks(
            user_id=str(row.user_id),
            chat_id=row.chat_id,
            click_count=int(row.click_count),
            last_click_at=row.last_click_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
