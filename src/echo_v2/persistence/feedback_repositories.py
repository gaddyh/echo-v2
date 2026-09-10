"""Repository protocols for feedback, actions, and chat mutes.

Clean separation:

* :class:`WaitingForMeFeedbackRepository` — stores correctness signals.
* :class:`WaitingForMeActionRepository` — stores user actions (state mutations).
* :class:`ChatMuteRepository` — stores and queries chat mutes.

All three are idempotent via ``UNIQUE(user_id, provider_event_id)`` on
feedback and actions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from echo_v2.domain.feedback import (
    ChatMute,
    FeedbackVerdict,
    WaitingForMeAction,
    WaitingForMeActionType,
    WaitingForMeFeedback,
)

__all__ = [
    "ChatMuteRepository",
    "InMemoryChatMuteRepository",
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
        provider_event_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> WaitingForMeFeedback | None:
        """Record a feedback verdict. Returns ``None`` if duplicate
        (same ``provider_event_id`` already recorded)."""
        ...

    async def count_recent_false_positives(
        self,
        *,
        user_id: str,
        chat_id: str,
        since: datetime,
    ) -> int:
        """Count false_positive verdicts for a chat since a timestamp.

        Used to decide whether to offer permanent mute after repeated
        dismissals.
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
        provider_event_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> WaitingForMeFeedback | None:
        # Idempotency: check for duplicate provider_event_id.
        if provider_event_id is not None:
            for row in self._rows:
                if (
                    row.user_id == user_id
                    and row.provider_event_id == provider_event_id
                ):
                    return None

        feedback = WaitingForMeFeedback(
            user_id=user_id,
            chat_id=chat_id,
            result_id=result_id,
            target_version=target_version,
            verdict=verdict,
            conversation_snapshot=conversation_snapshot,
            provider_event_id=provider_event_id,
            created_at=datetime.now(timezone.utc),
            expires_at=expires_at,
        )
        self._rows.append(feedback)
        return feedback

    async def count_recent_false_positives(
        self,
        *,
        user_id: str,
        chat_id: str,
        since: datetime,
    ) -> int:
        return sum(
            1
            for r in self._rows
            if r.user_id == user_id
            and r.chat_id == chat_id
            and r.verdict == FeedbackVerdict.FALSE_POSITIVE
            and r.created_at is not None
            and r.created_at >= since
        )

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
        provider_event_id: str | None = None,
    ) -> WaitingForMeAction | None:
        """Record an action. Returns ``None`` if duplicate
        (same ``provider_event_id`` already recorded)."""
        ...


class InMemoryWaitingForMeActionRepository:
    """Process-local action repository backed by a list."""

    def __init__(self) -> None:
        self._rows: list[WaitingForMeAction] = []

    async def record(
        self,
        *,
        user_id: str,
        chat_id: str,
        action_type: WaitingForMeActionType,
        active_id: str | None = None,
        target_version: int | None = None,
        action_payload: dict | None = None,
        provider_event_id: str | None = None,
    ) -> WaitingForMeAction | None:
        # Idempotency: check for duplicate provider_event_id.
        if provider_event_id is not None:
            for row in self._rows:
                if (
                    row.user_id == user_id
                    and row.provider_event_id == provider_event_id
                ):
                    return None

        action = WaitingForMeAction(
            user_id=user_id,
            chat_id=chat_id,
            active_id=active_id,
            target_version=target_version,
            action_type=action_type,
            action_payload=action_payload,
            provider_event_id=provider_event_id,
            created_at=datetime.now(timezone.utc),
        )
        self._rows.append(action)
        return action


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
