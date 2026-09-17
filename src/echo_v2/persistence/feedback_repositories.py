"""Repository protocols for feedback, actions, and chat mutes.

Clean separation:

* :class:`WaitingForMeFeedbackRepository` — stores correctness signals.
* :class:`WaitingForMeActionRepository` — stores user actions (state mutations).
* :class:`ChatMuteRepository` — stores and queries chat mutes.

All three are idempotent via ``UNIQUE(user_id, provider_message_id)`` on
feedback and actions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

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

if TYPE_CHECKING:
    from echo_v2.persistence.chat_repositories import (
        InMemoryWaitingForMeActiveRepository,
    )

__all__ = [
    "ChatMuteRepository",
    "ChatNotInterestedClickRepository",
    "InMemoryChatMuteRepository",
    "InMemoryChatNotInterestedClickRepository",
    "InMemoryWaitingForMeActionRepository",
    "InMemoryWaitingForMeFeedbackRepository",
    "WaitingForMeActionRepository",
    "WaitingForMeFeedbackRepository",
]


# --- Feedback repository ----------------------------------------------------


class WaitingForMeFeedbackRepository(Protocol):
    """Store correctness signals — was the model right?"""

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
        """Record a feedback verdict.

        Returns ``None`` if duplicate (either the same
        ``provider_message_id`` was already recorded, or a feedback for
        the same ``result_id`` already exists — first feedback wins).
        """
        ...

    async def delete_expired(self, *, now: datetime) -> int:
        """Delete feedback rows whose ``expires_at`` has passed.

        Returns the number of deleted rows.
        """
        ...


class InMemoryWaitingForMeFeedbackRepository:
    """Process-local feedback repository backed by a list."""

    def __init__(self) -> None:
        self._rows: list[WaitingForMeFeedback] = []

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
        # Idempotency: check for duplicate provider_message_id.
        if provider_message_id is not None:
            for row in self._rows:
                if (
                    row.user_id == user_id
                    and row.provider_message_id == provider_message_id
                ):
                    return None

        # Semantic dedup: first feedback for a result_id wins.
        if result_id is not None:
            for row in self._rows:
                if (
                    row.user_id == user_id
                    and row.result_id == result_id
                ):
                    return None

        feedback = WaitingForMeFeedback(
            user_id=user_id,
            chat_id=chat_id,
            result_id=result_id,
            target_version=target_version,
            verdict=verdict,
            conversation_snapshot=conversation_snapshot,
            provider_message_id=provider_message_id,
            created_at=datetime.now(timezone.utc),
            expires_at=expires_at,
        )
        self._rows.append(feedback)
        return feedback

    async def delete_expired(self, *, now: datetime) -> int:
        before = len(self._rows)
        self._rows = [
            r for r in self._rows
            if r.expires_at is None or r.expires_at > now
        ]
        return before - len(self._rows)


# --- Action repository ------------------------------------------------------


class WaitingForMeActionRepository(Protocol):
    """Store user actions on active items — what the user asked Echo to do."""

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
        """Record an action. Returns ``None`` if duplicate
        (same ``provider_message_id`` already recorded)."""
        ...

    async def list_by_session(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> list[WaitingForMeAction]:
        """List actions recorded for a waiting-list session.

        Filters by ``action_payload->>'waiting_list_session_id' == session_id``.
        Used to compute the per-session summary (completed/snoozed counts).
        """
        ...

    # --- Atomic command methods (record + mutate in one transaction) ---

    async def resolve_and_delete_by_version(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        provider_message_id: str,
        action_payload: dict | None = None,
    ) -> ActionCommandResult:
        """Record RESOLVE + delete active by version, atomically.

        Records the action (idempotent on ``provider_message_id``), then
        deletes the active row if ``id + user_id + target_version``
        match. Both operations run in one transaction — a crash between
        them can never leave an audit row without a state change (or
        vice versa).

        Returns:
            APPLIED with ``chat_id``/``result_id`` if deleted.
            DUPLICATE if the action was already recorded.
            STALE if the active item exists but version differs.
            NOT_FOUND if the active item doesn't exist.
        """
        ...

    async def resolve_and_delete_by_chat(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        provider_message_id: str,
        action_payload: dict | None = None,
    ) -> ActionCommandResult:
        """Record RESOLVE + delete active by chat_id, atomically.

        Like :meth:`resolve_and_delete_by_version` but the delete is by
        ``(user_id, chat_id)`` (unconditional on version). The active
        item is fetched first to verify ``user_id`` and
        ``target_version`` — returns STALE or NOT_FOUND if they don't
        match. Used by ``handled`` and ``dismiss_not_waiting`` which
        need the chat_id for feedback recording.
        """
        ...

    async def snooze_active(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        snoozed_until: datetime,
        provider_message_id: str,
        action_payload: dict | None = None,
    ) -> ActionCommandResult:
        """Record SNOOZE + apply snoozed_until, atomically.

        Records the action (idempotent on ``provider_message_id``), then
        applies ``snoozed_until`` to the active row if ``id + user_id +
        target_version`` match. Both in one transaction.

        Returns:
            APPLIED with ``chat_id`` if the snooze was applied.
            DUPLICATE if the action was already recorded.
            STALE if the active item exists but version differs.
            NOT_FOUND if the active item doesn't exist.
        """
        ...

    async def mute_chat_atomic(
        self,
        *,
        user_id: str,
        chat_id: str,
        permanent: bool,
        muted_until: datetime | None,
        provider_message_id: str,
    ) -> ActionCommandResult:
        """Record MUTE_CHAT + mute the chat, atomically.

        Records the action (idempotent on ``provider_message_id``), then
        mutes the chat (permanent or temporary). Both in one transaction.

        Returns APPLIED or DUPLICATE.
        """
        ...

    async def unmute_chat_atomic(
        self,
        *,
        user_id: str,
        chat_id: str,
        provider_message_id: str,
    ) -> ActionCommandResult:
        """Record UNMUTE_CHAT + unmute the chat, atomically.

        Records the action (idempotent on ``provider_message_id``), then
        unmutes the chat. Both in one transaction.

        Returns APPLIED or DUPLICATE.
        """
        ...


class InMemoryWaitingForMeActionRepository:
    """Process-local action repository backed by a list.

    The atomic command methods (``resolve_and_delete_by_version`` etc.)
    require ``active_repo`` and/or ``mute_repo`` to be wired at
    construction time. The standalone ``record()`` and
    ``list_by_session()`` methods work without them.
    """

    def __init__(
        self,
        *,
        active_repo: InMemoryWaitingForMeActiveRepository | None = None,
        mute_repo: InMemoryChatMuteRepository | None = None,
    ) -> None:
        self._rows: list[WaitingForMeAction] = []
        self._active_repo = active_repo
        self._mute_repo = mute_repo

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
        # Idempotency: check for duplicate provider_message_id.
        if provider_message_id is not None:
            for row in self._rows:
                if (
                    row.user_id == user_id
                    and row.provider_message_id == provider_message_id
                ):
                    return None

        action = WaitingForMeAction(
            user_id=user_id,
            chat_id=chat_id,
            active_id=active_id,
            target_version=target_version,
            action_type=action_type,
            action_payload=action_payload,
            provider_message_id=provider_message_id,
            created_at=datetime.now(timezone.utc),
        )
        self._rows.append(action)
        return action

    async def list_by_session(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> list[WaitingForMeAction]:
        """List actions for a waiting-list session."""
        return [
            row
            for row in self._rows
            if row.user_id == user_id
            and row.action_payload
            and row.action_payload.get("waiting_list_session_id") == session_id
        ]

    async def resolve_and_delete_by_version(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        provider_message_id: str,
        action_payload: dict | None = None,
    ) -> ActionCommandResult:
        assert self._active_repo is not None
        action = await self.record(
            user_id=user_id,
            chat_id="",
            action_type=WaitingForMeActionType.RESOLVE,
            active_id=active_id,
            target_version=target_version,
            action_payload=action_payload,
            provider_message_id=provider_message_id,
        )
        if action is None:
            return ActionCommandResult(outcome=HandlingOutcome.DUPLICATE)

        deleted = await self._active_repo.delete_if_version(
            active_id=active_id,
            user_id=user_id,
            target_version=target_version,
        )
        if deleted is not None:
            return ActionCommandResult(
                outcome=HandlingOutcome.APPLIED,
                chat_id=deleted.chat_id,
                result_id=deleted.result_id,
            )
        active = await self._active_repo.get_by_id(active_id)
        if active is None:
            return ActionCommandResult(outcome=HandlingOutcome.NOT_FOUND)
        return ActionCommandResult(outcome=HandlingOutcome.STALE)

    async def resolve_and_delete_by_chat(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        provider_message_id: str,
        action_payload: dict | None = None,
    ) -> ActionCommandResult:
        assert self._active_repo is not None
        action = await self.record(
            user_id=user_id,
            chat_id="",
            action_type=WaitingForMeActionType.RESOLVE,
            active_id=active_id,
            target_version=target_version,
            action_payload=action_payload,
            provider_message_id=provider_message_id,
        )
        if action is None:
            return ActionCommandResult(outcome=HandlingOutcome.DUPLICATE)

        active = await self._active_repo.get_by_id(active_id)
        if active is None:
            return ActionCommandResult(outcome=HandlingOutcome.NOT_FOUND)
        if active.user_id != user_id or active.target_version != target_version:
            return ActionCommandResult(outcome=HandlingOutcome.STALE)

        await self._active_repo.delete(user_id=user_id, chat_id=active.chat_id)
        return ActionCommandResult(
            outcome=HandlingOutcome.APPLIED,
            chat_id=active.chat_id,
            result_id=active.result_id,
        )

    async def snooze_active(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        snoozed_until: datetime,
        provider_message_id: str,
        action_payload: dict | None = None,
    ) -> ActionCommandResult:
        assert self._active_repo is not None
        action = await self.record(
            user_id=user_id,
            chat_id="",
            action_type=WaitingForMeActionType.SNOOZE,
            active_id=active_id,
            target_version=target_version,
            action_payload=action_payload,
            provider_message_id=provider_message_id,
        )
        if action is None:
            return ActionCommandResult(outcome=HandlingOutcome.DUPLICATE)

        changed = await self._active_repo.apply_if_version(
            active_id=active_id,
            user_id=user_id,
            target_version=target_version,
            mutate={"snoozed_until": snoozed_until},
        )
        if changed:
            active = await self._active_repo.get_by_id(active_id)
            return ActionCommandResult(
                outcome=HandlingOutcome.APPLIED,
                chat_id=active.chat_id if active else None,
            )
        active = await self._active_repo.get_by_id(active_id)
        if active is None:
            return ActionCommandResult(outcome=HandlingOutcome.NOT_FOUND)
        return ActionCommandResult(outcome=HandlingOutcome.STALE)

    async def mute_chat_atomic(
        self,
        *,
        user_id: str,
        chat_id: str,
        permanent: bool,
        muted_until: datetime | None,
        provider_message_id: str,
    ) -> ActionCommandResult:
        assert self._mute_repo is not None
        action = await self.record(
            user_id=user_id,
            chat_id=chat_id,
            action_type=WaitingForMeActionType.MUTE_CHAT,
            provider_message_id=provider_message_id,
        )
        if action is None:
            return ActionCommandResult(outcome=HandlingOutcome.DUPLICATE)

        if permanent:
            await self._mute_repo.mute_permanent(user_id=user_id, chat_id=chat_id)
        else:
            assert muted_until is not None
            await self._mute_repo.mute_temporary(
                user_id=user_id, chat_id=chat_id, muted_until=muted_until,
            )
        return ActionCommandResult(outcome=HandlingOutcome.APPLIED)

    async def unmute_chat_atomic(
        self,
        *,
        user_id: str,
        chat_id: str,
        provider_message_id: str,
    ) -> ActionCommandResult:
        assert self._mute_repo is not None
        action = await self.record(
            user_id=user_id,
            chat_id=chat_id,
            action_type=WaitingForMeActionType.UNMUTE_CHAT,
            provider_message_id=provider_message_id,
        )
        if action is None:
            return ActionCommandResult(outcome=HandlingOutcome.DUPLICATE)

        await self._mute_repo.unmute(user_id=user_id, chat_id=chat_id)
        return ActionCommandResult(outcome=HandlingOutcome.APPLIED)


# --- Chat mute repository ---------------------------------------------------


class ChatMuteRepository(Protocol):
    """Query and manage per-user, per-chat mutes."""

    async def is_muted(
        self,
        *,
        user_id: str,
        chat_id: str,
        now: datetime,
    ) -> bool:
        """Check if a chat is currently muted for a user."""
        ...

    async def mute_temporary(
        self,
        *,
        user_id: str,
        chat_id: str,
        muted_until: datetime,
    ) -> None:
        """Create or update a temporary mute."""
        ...

    async def mute_permanent(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> None:
        """Create or update a permanent mute."""
        ...

    async def unmute(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> bool:
        """Remove a mute. Returns ``True`` if a row was deleted."""
        ...

    async def get(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> ChatMute | None:
        """Get the mute record for a chat, or ``None``."""
        ...


class InMemoryChatMuteRepository:
    """Process-local mute repository backed by a dict."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], ChatMute] = {}

    async def is_muted(
        self,
        *,
        user_id: str,
        chat_id: str,
        now: datetime,
    ) -> bool:
        mute = self._rows.get((user_id, chat_id))
        if mute is None:
            return False
        if mute.permanent:
            return True
        if mute.muted_until is not None and mute.muted_until > now:
            return True
        # Expired temporary mute — clean up.
        if mute.muted_until is not None and mute.muted_until <= now:
            del self._rows[(user_id, chat_id)]
            return False
        return False

    async def mute_temporary(
        self,
        *,
        user_id: str,
        chat_id: str,
        muted_until: datetime,
    ) -> None:
        self._rows[(user_id, chat_id)] = ChatMute(
            user_id=user_id,
            chat_id=chat_id,
            muted_until=muted_until,
            permanent=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    async def mute_permanent(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> None:
        self._rows[(user_id, chat_id)] = ChatMute(
            user_id=user_id,
            chat_id=chat_id,
            muted_until=None,
            permanent=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    async def unmute(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> bool:
        key = (user_id, chat_id)
        if key in self._rows:
            del self._rows[key]
            return True
        return False

    async def get(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> ChatMute | None:
        return self._rows.get((user_id, chat_id))


# --- Chat not-interested click counter --------------------------------------


class ChatNotInterestedClickRepository(Protocol):
    """Per-user, per-chat counter for "לא מעניין, שיחכו" clicks.

    Drives the escalating chat snooze (24h → 48h → 1 week → permanent).
    Reset (row deleted) when the user engages with the chat.
    """

    async def increment(
        self,
        *,
        user_id: str,
        chat_id: str,
        now: datetime,
    ) -> int:
        """Increment the click counter and return the new count."""
        ...

    async def reset(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> None:
        """Reset the counter (delete the row)."""
        ...

    async def get(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> ChatNotInterestedClicks | None:
        """Get the current click record, or ``None``."""
        ...


class InMemoryChatNotInterestedClickRepository:
    """Process-local click counter backed by a dict."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], ChatNotInterestedClicks] = {}

    async def increment(
        self,
        *,
        user_id: str,
        chat_id: str,
        now: datetime,
    ) -> int:
        key = (user_id, chat_id)
        existing = self._rows.get(key)
        new_count = (existing.click_count + 1) if existing else 1
        row = ChatNotInterestedClicks(
            user_id=user_id,
            chat_id=chat_id,
            click_count=new_count,
            last_click_at=now,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self._rows[key] = row
        return new_count

    async def reset(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> None:
        self._rows.pop((user_id, chat_id), None)

    async def get(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> ChatNotInterestedClicks | None:
        return self._rows.get((user_id, chat_id))
