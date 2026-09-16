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

Two entry points:

* :meth:`current_actionable` — returns raw :class:`WaitingForMeActive`
  rows (used by surfaces that need the raw row, e.g. for version checks).
* :meth:`current_views` — returns fully-resolved
  :class:`WaitingForMeView` read models (used by the mini-app and the
  WhatsApp bot cards). This is the canonical read path; no surface
  should resolve names/previews/summaries itself.
* :meth:`count_actionable` — returns just the count of actionable
  items (used by the digest worker, which only needs the count for
  the template).
"""

from __future__ import annotations

from datetime import datetime, timezone

from echo_v2.domain.waiting_for_me import WaitingForMeActive
from echo_v2.persistence.chat_repositories import (
    ChatStateRepository,
    MessageRepository,
    WaitingForMeActiveRepository,
    WaitingForMeResultRepository,
)
from echo_v2.persistence.contacts import ContactRecord, ContactRepository
from echo_v2.persistence.feedback_repositories import ChatMuteRepository
from echo_v2.services.waiting_for_me_view import (
    ContactNameResolver,
    WaitingForMeView,
    phone_from_chat_id,
    truncate_preview,
)

__all__ = ["WaitingListQueryService"]


class WaitingListQueryService:
    """Shared query for current actionable waiting items.

    Args:
        active_repo: The :class:`WaitingForMeActiveRepository`.
        chat_state_repo: The :class:`ChatStateRepository` for version checks.
        mute_repo: Optional :class:`ChatMuteRepository` for mute filtering.
        message_repo: Optional :class:`MessageRepository` for previews +
            name fallback. Required for :meth:`current_views`.
        contact_repo: Optional :class:`ContactRepository` for name
            resolution + star/label/tags. Required for
            :meth:`current_views`.
        result_repo: Optional :class:`WaitingForMeResultRepository` for
            situation summaries. When ``None``, ``summary`` is always
            ``None`` on the returned views.
    """

    def __init__(
        self,
        *,
        active_repo: WaitingForMeActiveRepository,
        chat_state_repo: ChatStateRepository,
        mute_repo: ChatMuteRepository | None = None,
        message_repo: MessageRepository | None = None,
        contact_repo: ContactRepository | None = None,
        result_repo: WaitingForMeResultRepository | None = None,
    ) -> None:
        self._active_repo = active_repo
        self._chat_state_repo = chat_state_repo
        self._mute_repo = mute_repo
        self._message_repo = message_repo
        self._contact_repo = contact_repo
        self._result_repo = result_repo
        self._resolver: ContactNameResolver | None = None
        if message_repo is not None and contact_repo is not None:
            self._resolver = ContactNameResolver(
                chat_state_repo=chat_state_repo,
                message_repo=message_repo,
                contact_repo=contact_repo,
            )

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

    async def count_actionable(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> int:
        """Count actionable waiting items for a user.

        Applies the same filtering as :meth:`current_actionable` but
        returns only the count — no list construction, no name/preview
        resolution. Used by the digest worker, which only needs the
        count for the template.
        """
        # Reuse current_actionable — the filtering logic is identical.
        # A future optimization could push the count into the repository
        # layer, but the per-user active set is small enough that this
        # is not a bottleneck today.
        return len(await self.current_actionable(user_id, now=now))

    async def current_views(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> list[WaitingForMeView]:
        """Get fully-resolved views of currently actionable waiting items.

        This is the canonical read path consumed by the mini-app and the
        WhatsApp bot cards. Items are sorted: starred contacts first
        (oldest first), then non-starred (oldest first).

        Requires ``message_repo``, ``contact_repo`` (and ideally
        ``result_repo`` for summaries) to be wired at construction time.
        """
        if self._resolver is None:
            raise RuntimeError(
                "current_views requires message_repo and contact_repo "
                "to be wired into WaitingListQueryService"
            )

        now = now or datetime.now(timezone.utc)
        actives = await self.current_actionable(user_id, now=now)

        # Batch-fetch contact metadata (star, color_label, tags) for all
        # phones in a single query.
        phones = {phone_from_chat_id(a.chat_id) for a in actives}
        contact_meta = (
            await self._contact_repo.get_contact_metadata(user_id, phones)
            if self._contact_repo is not None
            else {}
        )
        starred_phones = {p for p, c in contact_meta.items() if c.is_starred}

        # Sort: starred contacts first (oldest first), then non-starred
        # (oldest first).
        actives.sort(
            key=lambda active: (
                phone_from_chat_id(active.chat_id) not in starred_phones,
                active.waiting_since,
            )
        )

        views: list[WaitingForMeView] = []
        for active in actives:
            view = await self._build_view(
                user_id, active, contact_meta=contact_meta
            )
            views.append(view)
        return views

    async def build_view_for_active(
        self,
        user_id: str,
        active: WaitingForMeActive,
    ) -> WaitingForMeView:
        """Build a single view for an already-fetched active row.

        Used by the action service to rebuild the item state for a stale
        action response. Does NOT re-run the actionable filter and does
        NOT apply the starred-first sort — it reflects the given row as-is.
        """
        if self._resolver is None:
            raise RuntimeError(
                "build_view_for_active requires message_repo and "
                "contact_repo to be wired into WaitingListQueryService"
            )
        phone = phone_from_chat_id(active.chat_id)
        contact_meta = (
            await self._contact_repo.get_contact_metadata(user_id, {phone})
            if self._contact_repo is not None
            else {}
        )
        return await self._build_view(user_id, active, contact_meta=contact_meta)

    async def _build_view(
        self,
        user_id: str,
        active: WaitingForMeActive,
        *,
        contact_meta: dict[str, ContactRecord],
    ) -> WaitingForMeView:
        """Build a WaitingForMeView for one active row."""
        assert self._resolver is not None  # checked by callers
        phone = phone_from_chat_id(active.chat_id)

        contact = contact_meta.get(phone)
        msg = await self._resolver.get_latest_inbound(user_id, active.chat_id)
        contact_name = await self._resolver.resolve_name(
            user_id, active.chat_id, contact=contact, msg=msg
        )

        last_message = truncate_preview(msg.text if msg and msg.text else None)

        summary: str | None = None
        if self._result_repo is not None and active.result_id:
            result = await self._result_repo.get_by_id(active.result_id)
            if result is not None:
                summary = result.summary

        is_starred = contact.is_starred if contact else False
        color_label = contact.color_label if contact else None
        tags = list(contact.tags) if contact else []

        return WaitingForMeView(
            id=active.id,
            owner=active.user_id,
            contact_name=contact_name,
            summary=summary,
            last_message=last_message,
            waiting_since=active.waiting_since,
            version=active.target_version,
            is_starred=is_starred,
            color_label=color_label,
            tags=tags,
        )
