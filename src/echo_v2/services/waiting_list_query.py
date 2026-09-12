"""WaitingListQueryService — shared "current actionable items" query.

Extracted from the duplicated logic in ``DigestWorker._get_current_active``
and ``FeedbackHandler._handle_view_details``. Both the digest worker and
the waiting-list web service use this shared service so they never
disagree on what's actionable.

"Actionable" = active waiting items where:
* ``target_version == chats.activity_version`` (not stale)
* ``acknowledged_at IS NULL`` (not acknowledged)
* ``snoozed_until IS NULL OR snoozed_until <= now`` (not currently snoozed)
* not muted (``chat_mutes`` has no active row for this chat)
"""

from __future__ import annotations

from datetime import datetime, timezone

from echo_v2.domain.waiting_for_me import WaitingForMeActive
from echo_v2.persistence.chat_repositories import (
    ChatStateRepository,
    WaitingForMeActiveRepository,
)
from echo_v2.persistence.feedback_repositories import ChatMuteRepository

__all__ = ["WaitingListQueryService"]


class WaitingListQueryService:
    """Shared query for current actionable waiting items.

    Args:
        active_repo: The :class:`WaitingForMeActiveRepository`.
        chat_state_repo: The :class:`ChatStateRepository` for version checks.
        mute_repo: Optional :class:`ChatMuteRepository` for mute filtering.
    """

    def __init__(
        self,
        *,
        active_repo: WaitingForMeActiveRepository,
        chat_state_repo: ChatStateRepository,
        mute_repo: ChatMuteRepository | None = None,
    ) -> None:
        self._active_repo = active_repo
        self._chat_state_repo = chat_state_repo
        self._mute_repo = mute_repo

    async def current_actionable(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> list[WaitingForMeActive]:
        """Get active waiting items that are currently actionable.

        Returns items sorted by ``waiting_since`` ascending (oldest first).
        """
        now = now or datetime.now(timezone.utc)
        all_active = await self._active_repo.list_all_for_user(user_id=user_id)
        current: list[WaitingForMeActive] = []
        for active in all_active:
            chat = await self._chat_state_repo.get(user_id, active.chat_id)
            if chat is None or chat.activity_version != active.target_version:
                continue
            # Skip acknowledged items.
            if active.acknowledged_at is not None:
                continue
            # Skip snoozed items.
            if active.snoozed_until is not None and active.snoozed_until > now:
                continue
            # Skip muted chats.
            if self._mute_repo is not None and await self._mute_repo.is_muted(
                user_id=user_id, chat_id=active.chat_id, now=now
            ):
                continue
            current.append(active)
        current.sort(key=lambda a: a.waiting_since)
        return current
