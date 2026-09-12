"""WaitingListService — list items + execute actions for the mini web app.

The service layer between the FastAPI routes and the action service.
It validates the session, resolves the user, queries actionable items
via the shared :class:`WaitingListQueryService`, and dispatches actions
to :class:`WaitingForMeActionService`.

Per-session summary counts (completed, snoozed) are computed from the
``waiting_for_me_actions`` table by ``waiting_list_session_id`` in the
``action_payload``. This survives page refresh/reopen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from echo_v2.domain.feedback import HandlingOutcome, WaitingForMeActionType
from echo_v2.domain.waiting_for_me import WaitingForMeActive
from echo_v2.persistence.chat_repositories import (
    ChatStateRepository,
    MessageRepository,
)
from echo_v2.persistence.contacts import ContactRepository
from echo_v2.persistence.feedback_repositories import WaitingForMeActionRepository
from echo_v2.services.feedback_service import WaitingForMeActionService
from echo_v2.services.waiting_list_query import WaitingListQueryService
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

__all__ = [
    "ActionResponse",
    "WaitingListItem",
    "WaitingListResponse",
    "WaitingListService",
    "WaitingListSummary",
]

_logger = logging.getLogger("echo_v2.services.waiting_list")

# Max preview length for customer messages (truncated server-side).
MAX_PREVIEW_CHARS = 120


@dataclass(frozen=True)
class WaitingListItem:
    """One actionable waiting item in the list.

    Attributes:
        active_id: The surrogate UUID of the active row. Used as the
            item identifier in action requests.
        contact_name: Resolved contact name, or ``None`` if unknown.
        message_preview: Truncated last inbound message text (max 120
            chars), or ``None`` if media-only or no text.
        waiting_since: When the waiting state originally started.
        waiting_hours: Hours since ``waiting_since`` (float).
        expected_version: The ``target_version`` the client should send
            back in action requests for concurrency control.
    """

    active_id: str
    contact_name: str | None
    message_preview: str | None
    waiting_since: datetime
    waiting_hours: float
    expected_version: int


@dataclass(frozen=True)
class WaitingListSummary:
    """Server-authoritative per-session summary.

    Attributes:
        waiting: Number of currently actionable waiting items.
        snoozed: Number of snooze actions in this session.
        completed: Number of resolve actions (done + dismiss) in this
            session.
    """

    waiting: int
    snoozed: int
    completed: int


@dataclass(frozen=True)
class WaitingListResponse:
    """Response for GET /api/waiting."""

    items: list[WaitingListItem]
    summary: WaitingListSummary


@dataclass(frozen=True)
class ActionResponse:
    """Response for POST /api/waiting/items/{active_id}/actions.

    Attributes:
        outcome: The handling outcome string ("applied", "duplicate",
            "stale", "not_found", "invalid_snooze").
        action: The action that was requested.
        item: The current item state (only for stale responses, so the
            client can update its view). ``None`` otherwise.
        summary: The updated per-session summary.
    """

    outcome: str
    action: str
    item: WaitingListItem | None
    summary: WaitingListSummary


class WaitingListService:
    """List waiting items and execute actions for the mini web app.

    Args:
        token_service: The :class:`WaitingListTokenService` for session
            validation.
        query_service: The shared :class:`WaitingListQueryService`.
        action_service: The :class:`WaitingForMeActionService` for
            executing actions.
        action_repo: The :class:`WaitingForMeActionRepository` for
            per-session summary counts.
        chat_state_repo: The :class:`ChatStateRepository` for name
            resolution.
        message_repo: The :class:`MessageRepository` for last message
            text.
        contact_repo: The :class:`ContactRepository` for name resolution.
        tz_name: Default timezone for snooze presets.
    """

    def __init__(
        self,
        *,
        token_service: WaitingListTokenService,
        query_service: WaitingListQueryService,
        action_service: WaitingForMeActionService,
        action_repo: WaitingForMeActionRepository,
        chat_state_repo: ChatStateRepository,
        message_repo: MessageRepository,
        contact_repo: ContactRepository,
        tz_name: str = "Asia/Jerusalem",
    ) -> None:
        self._token_service = token_service
        self._query_service = query_service
        self._action_service = action_service
        self._action_repo = action_repo
        self._chat_state_repo = chat_state_repo
        self._message_repo = message_repo
        self._contact_repo = contact_repo
        self._tz_name = tz_name

    async def list_items(
        self,
        session_id: str,
        user_id: str,
    ) -> WaitingListResponse | None:
        """List actionable waiting items for a session.

        Returns ``None`` if the session is invalid/expired/revoked.
        """
        # Validate session.
        resolved = await self._token_service.resolve_session(session_id)
        if resolved is None or resolved.user_id != user_id:
            return None

        now = datetime.now(timezone.utc)
        actives = await self._query_service.current_actionable(user_id, now=now)

        items = []
        for active in actives:
            item = await self._build_item(user_id, active, now)
            items.append(item)

        summary = await self._build_summary(session_id, user_id, len(items))
        return WaitingListResponse(items=items, summary=summary)

    async def execute_action(
        self,
        session_id: str,
        user_id: str,
        active_id: str,
        action_id: str,
        expected_version: int,
        action: str,
        snooze_preset: str | None = None,
        snooze_until: datetime | None = None,
        dismiss_reason: str | None = None,
    ) -> ActionResponse | None:
        """Execute an action on a waiting item.

        Returns ``None`` if the session is invalid/expired/revoked.
        """
        # Validate session.
        resolved = await self._token_service.resolve_session(session_id)
        if resolved is None or resolved.user_id != user_id:
            return None

        # Touch the session.
        await self._token_service.touch(session_id)

        provider_message_id = f"web:{action_id}"

        if action == "done":
            outcome = await self._action_service.done(
                user_id=user_id,
                active_id=active_id,
                target_version=expected_version,
                provider_message_id=provider_message_id,
                session_id=session_id,
            )
        elif action == "snooze":
            outcome = await self._action_service.snooze(
                user_id=user_id,
                active_id=active_id,
                target_version=expected_version,
                provider_message_id=provider_message_id,
                tz_name=self._tz_name,
                snooze_preset=snooze_preset,
                snooze_until=snooze_until,
                session_id=session_id,
            )
        elif action == "dismiss":
            reason = dismiss_reason or "no_response_required"
            outcome = await self._action_service.dismiss_with_reason(
                user_id=user_id,
                active_id=active_id,
                target_version=expected_version,
                provider_message_id=provider_message_id,
                reason=reason,
                session_id=session_id,
            )
        else:
            return ActionResponse(
                outcome="invalid",
                action=action,
                item=None,
                summary=await self._build_summary(session_id, user_id),
            )

        # Map HandlingOutcome to string.
        outcome_str = _outcome_to_str(outcome)

        # For stale, include the current item state.
        item = None
        if outcome == HandlingOutcome.STALE:
            active = await self._action_service._active_repo.get_by_id(active_id)
            if active is not None:
                now = datetime.now(timezone.utc)
                item = await self._build_item(user_id, active, now)

        summary = await self._build_summary(session_id, user_id)
        return ActionResponse(
            outcome=outcome_str,
            action=action,
            item=item,
            summary=summary,
        )

    async def _build_item(
        self,
        user_id: str,
        active: WaitingForMeActive,
        now: datetime,
    ) -> WaitingListItem:
        """Build a WaitingListItem with name resolution + preview."""
        phone = _phone_from_chat_id(active.chat_id)

        # Name resolution: chat_name → contact → message names.
        chat = await self._chat_state_repo.get(user_id, active.chat_id)
        chat_name = chat.chat_name if chat else None

        if not chat_name:
            contact = await self._contact_repo.find_by_phone(user_id, phone)
            chat_name = contact.display_name if contact else None

        msg = await self._message_repo.get_latest_inbound(
            user_id=user_id,
            chat_id=active.chat_id,
        )
        last_text = msg.text if msg and msg.text else None

        if not chat_name and msg:
            chat_name = msg.chat_name or msg.sender_name

        # Truncate preview.
        preview = None
        if last_text:
            preview = last_text[:MAX_PREVIEW_CHARS]
            if len(last_text) > MAX_PREVIEW_CHARS:
                preview = preview + "…"

        waiting_hours = (now - active.waiting_since).total_seconds() / 3600.0

        return WaitingListItem(
            active_id=active.id,
            contact_name=chat_name,
            message_preview=preview,
            waiting_since=active.waiting_since,
            waiting_hours=round(waiting_hours, 1),
            expected_version=active.target_version,
        )

    async def _build_summary(
        self,
        session_id: str,
        user_id: str,
        waiting_count: int | None = None,
    ) -> WaitingListSummary:
        """Build the per-session summary.

        If ``waiting_count`` is provided, use it (avoids a re-query).
        Otherwise, query current actionable items.
        """
        if waiting_count is None:
            now = datetime.now(timezone.utc)
            actives = await self._query_service.current_actionable(user_id, now=now)
            waiting_count = len(actives)

        # Count actions by type for this session.
        actions = await self._action_repo.list_by_session(
            user_id=user_id,
            session_id=session_id,
        )
        snoozed = sum(
            1 for a in actions if a.action_type == WaitingForMeActionType.SNOOZE
        )
        completed = sum(
            1 for a in actions if a.action_type == WaitingForMeActionType.RESOLVE
        )

        return WaitingListSummary(
            waiting=waiting_count,
            snoozed=snoozed,
            completed=completed,
        )


def _outcome_to_str(outcome: HandlingOutcome) -> str:
    """Map HandlingOutcome enum to the API response string."""
    mapping = {
        HandlingOutcome.APPLIED: "applied",
        HandlingOutcome.DUPLICATE: "duplicate",
        HandlingOutcome.STALE: "stale",
        HandlingOutcome.NOT_FOUND: "not_found",
        HandlingOutcome.INVALID: "invalid_snooze",
    }
    return mapping.get(outcome, "invalid")


def _phone_from_chat_id(chat_id: str) -> str:
    """Extract phone number from a WhatsApp chat ID."""
    return chat_id.split("@")[0] if "@" in chat_id else chat_id
