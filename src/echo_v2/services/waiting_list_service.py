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
from typing import Literal

from echo_v2.domain.feedback import HandlingOutcome, WaitingForMeActionType
from echo_v2.domain.scheduling import ScheduledActionType
from echo_v2.domain.waiting_for_me import WaitingForMeActive
from echo_v2.persistence.chat_repositories import (
    ChatStateRepository,
    MessageRepository,
    WaitingForMeActiveRepository,
    WaitingForMeResultRepository,
)
from echo_v2.persistence.contacts import ContactRepository
from echo_v2.persistence.feedback_repositories import WaitingForMeActionRepository
from echo_v2.services.feedback_service import WaitingForMeActionService
from echo_v2.services.scheduling import SchedulingService
from echo_v2.services.time_presets import preset_to_utc
from echo_v2.services.waiting_list_query import WaitingListQueryService
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

__all__ = [
    "ActionResponse",
    "SendResponse",
    "StarResponse",
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
        situation_summary: One-sentence Hebrew summary from the LLM
            analysis, describing the situation and what is waited for.
            ``None`` if the analysis predates summaries.
        message_preview: Truncated last inbound message text (max 120
            chars), or ``None`` if media-only or no text.
        waiting_since: When the waiting state originally started.
        waiting_hours: Hours since ``waiting_since`` (float).
        expected_version: The ``target_version`` the client should send
            back in action requests for concurrency control.
    """

    active_id: str
    contact_name: str | None
    situation_summary: str | None
    message_preview: str | None
    waiting_since: datetime
    waiting_hours: float
    expected_version: int
    is_starred: bool = False


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


@dataclass(frozen=True)
class SendResponse:
    """Response for POST /api/waiting/items/{active_id}/send.

    Attributes:
        outcome: ``"scheduled"`` if a new action was created,
            ``"duplicate"`` if the same request_id was retried,
            ``"not_found"`` if the active item is missing or not owned by
            the user, ``"invalid"`` if the message or timing is invalid.
        scheduled_for: ISO 8601 UTC datetime when the message will be
            sent. ``None`` for non-scheduled outcomes.
        action_id: The id of the ScheduledAction. ``None`` for
            non-scheduled outcomes.
    """

    outcome: Literal["scheduled", "duplicate", "not_found", "invalid"]
    scheduled_for: str | None = None
    action_id: str | None = None


@dataclass(frozen=True)
class StarResponse:
    """Response for POST /api/waiting/items/{active_id}/star.

    Attributes:
        outcome: ``"updated"`` if the star was set successfully,
            ``"not_found"`` if the active item is missing or not owned
            by the user.
        is_starred: The new ``is_starred`` value. ``None`` for
            ``not_found`` outcomes.
    """

    outcome: Literal["updated", "not_found"]
    is_starred: bool | None = None


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
        scheduling_service: Optional :class:`SchedulingService` for
            scheduling WhatsApp messages from the web app.
        active_repo: Optional :class:`WaitingForMeActiveRepository` for
            resolving active items when scheduling messages.
        tz_name: Default timezone for snooze/send presets.
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
        result_repo: WaitingForMeResultRepository | None = None,
        scheduling_service: SchedulingService | None = None,
        active_repo: WaitingForMeActiveRepository | None = None,
        tz_name: str = "Asia/Jerusalem",
    ) -> None:
        self._token_service = token_service
        self._query_service = query_service
        self._action_service = action_service
        self._action_repo = action_repo
        self._chat_state_repo = chat_state_repo
        self._message_repo = message_repo
        self._contact_repo = contact_repo
        self._result_repo = result_repo
        self._scheduling_service = scheduling_service
        self._active_repo = active_repo
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

        # Sort: starred contacts first (oldest first), then non-starred
        # (oldest first). Single query for all starred phones.
        starred_phones = await self._contact_repo.list_starred_phones(user_id)
        actives.sort(
            key=lambda active: (
                _phone_from_chat_id(active.chat_id) not in starred_phones,
                active.waiting_since,
            )
        )

        items = []
        for active in actives:
            item = await self._build_item(user_id, active, now, starred_phones)
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

    async def schedule_send(
        self,
        session_id: str,
        user_id: str,
        active_id: str,
        request_id: str,
        message: str,
        send_preset: str | None = None,
        send_at: datetime | None = None,
    ) -> SendResponse | None:
        """Schedule a WhatsApp message to the contact of a waiting item.

        Resolves the recipient (``chat_id``) server-side from the active
        item — never trusts a client-supplied phone number. Creates a
        ``SEND_WHATSAPP_MESSAGE`` scheduled action via
        :meth:`SchedulingService.create_once` so a retry with the same
        ``request_id`` returns the original action instead of creating a
        duplicate.

        Returns ``None`` if the session is invalid/expired/revoked.
        Returns a :class:`SendResponse` with ``outcome="not_found"`` if
        the active item is missing or owned by another user, or
        ``outcome="invalid"`` if the message/timing is invalid.
        """
        # Validate session.
        resolved = await self._token_service.resolve_session(session_id)
        if resolved is None or resolved.user_id != user_id:
            return None

        # Touch the session.
        await self._token_service.touch(session_id)

        if self._scheduling_service is None or self._active_repo is None:
            return SendResponse(outcome="invalid")

        # Resolve the active item server-side.
        active = await self._active_repo.get_by_id(active_id)
        if active is None or active.user_id != user_id:
            return SendResponse(outcome="not_found")

        # Validate message.
        if not message or not message.strip():
            return SendResponse(outcome="invalid")

        # Resolve timing: exactly one of send_preset / send_at.
        now = datetime.now(timezone.utc)
        if send_preset is not None and send_at is not None:
            return SendResponse(outcome="invalid")
        if send_preset is None and send_at is None:
            return SendResponse(outcome="invalid")

        if send_preset is not None:
            try:
                execute_at_utc = preset_to_utc(
                    send_preset, now_utc=now, tz_name=self._tz_name
                )
            except ValueError:
                return SendResponse(outcome="invalid")
        else:
            assert send_at is not None
            # send_at must be offset-aware (Pydantic enforces at the API
            # boundary; service callers must also pass aware datetimes).
            if send_at.tzinfo is None:
                return SendResponse(outcome="invalid")
            execute_at_utc = send_at.astimezone(timezone.utc)
            if execute_at_utc <= now:
                return SendResponse(outcome="invalid")

        action, created = await self._scheduling_service.create_once(
            request_id=request_id,
            user_id=user_id,
            type=ScheduledActionType.SEND_WHATSAPP_MESSAGE,
            execute_at_utc=execute_at_utc,
            timezone_name=self._tz_name,
            payload={
                "chat_id": active.chat_id,
                "message": message,
                "source": "waiting_list_web",
                "active_id": active_id,
            },
        )
        return SendResponse(
            outcome="scheduled" if created else "duplicate",
            scheduled_for=execute_at_utc.isoformat(),
            action_id=action.id,
        )

    async def set_starred(
        self,
        session_id: str,
        user_id: str,
        active_id: str,
        is_starred: bool,
    ) -> StarResponse | None:
        """Set ``is_starred`` on the contact associated with a waiting item.

        Returns ``None`` if the session is invalid/expired/revoked.
        Returns ``StarResponse(outcome="not_found")`` if the active item
        is missing or not owned by the user.

        The star is stored on the contact (by phone number), not on the
        waiting item. If no contact record exists, one is created with
        the resolved display name.
        """
        resolved = await self._token_service.resolve_session(session_id)
        if resolved is None or resolved.user_id != user_id:
            return None

        await self._token_service.touch(session_id)

        active = await self._active_repo.get_by_id(active_id)
        if active is None or active.user_id != user_id:
            return StarResponse(outcome="not_found")

        phone = _phone_from_chat_id(active.chat_id)

        # Resolve a display name for potential contact creation.
        chat = await self._chat_state_repo.get(user_id, active.chat_id)
        display_name = chat.chat_name if chat else None
        if not display_name:
            contact = await self._contact_repo.find_by_phone(user_id, phone)
            display_name = contact.display_name if contact else None
        if not display_name:
            msg = await self._message_repo.get_latest_inbound(
                user_id=user_id, chat_id=active.chat_id
            )
            display_name = msg.chat_name or msg.sender_name if msg else None
        if not display_name:
            display_name = phone

        await self._contact_repo.set_starred(
            user_id=user_id,
            phone=phone,
            is_starred=is_starred,
            display_name=display_name,
        )
        return StarResponse(outcome="updated", is_starred=is_starred)

    async def _build_item(
        self,
        user_id: str,
        active: WaitingForMeActive,
        now: datetime,
        starred_phones: set[str] | None = None,
    ) -> WaitingListItem:
        """Build a WaitingListItem with name resolution + preview + summary.

        Args:
            starred_phones: Optional pre-fetched set of starred phone
                numbers for this user. If provided, avoids a per-item
                ``find_by_phone`` call for the star state.
        """
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

        # Fetch the analysis result to get the situation summary.
        situation_summary = None
        if self._result_repo is not None and active.result_id:
            result = await self._result_repo.get_by_id(active.result_id)
            if result is not None:
                situation_summary = result.summary

        waiting_hours = (now - active.waiting_since).total_seconds() / 3600.0

        # Resolve is_starred from the pre-fetched set or via find_by_phone.
        if starred_phones is not None:
            is_starred = phone in starred_phones
        else:
            contact = await self._contact_repo.find_by_phone(user_id, phone)
            is_starred = contact.is_starred if contact else False

        return WaitingListItem(
            active_id=active.id,
            contact_name=chat_name,
            situation_summary=situation_summary,
            message_preview=preview,
            waiting_since=active.waiting_since,
            waiting_hours=round(waiting_hours, 1),
            expected_version=active.target_version,
            is_starred=is_starred,
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
