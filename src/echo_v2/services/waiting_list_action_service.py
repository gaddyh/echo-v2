"""WaitingListActionService — list items + execute actions for the mini web app.

The service layer between the FastAPI routes and the action service. It
validates the session, resolves the user, lists items via the shared
:class:`WaitingListQueryService` (the canonical read path), and dispatches
actions to :class:`WaitingForMeActionService`.

Per-session summary counts (completed, snoozed) are computed from the
``waiting_for_me_actions`` table by ``waiting_list_session_id`` in the
``action_payload``. This survives page refresh/reopen.

Name resolution for contact creation (in ``set_starred``/``set_label``/
``set_tags``) is delegated to the shared :class:`ContactNameResolver` —
the cascade ``chat_name → contact.display_name → message.sender_name →
phone`` lives once, in the resolver, not three times here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol, runtime_checkable

from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from echo_v2.domain.feedback import HandlingOutcome, WaitingForMeActionType
from echo_v2.domain.scheduling import ScheduledActionType
from echo_v2.observability.privacy import correlation_id
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
from echo_v2.services.waiting_for_me_view import (
    ContactNameResolver,
    WaitingForMeView,
    phone_from_chat_id,
)
from echo_v2.services.waiting_list_query import WaitingListQueryService
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

__all__ = [
    "ActionResponse",
    "ContextMessage",
    "SendResponse",
    "StarResponse",
    "WaitingListActionService",
    "WaitingListItem",
    "WaitingListResponse",
    "WaitingListSummary",
]

_logger = logging.getLogger("echo_v2.services.waiting_list")


@runtime_checkable
class UserInfoResolver(Protocol):
    """Resolve user_id to (phone, first_name) for trace metadata."""

    async def get_user_info_by_id(
        self, user_id: str
    ) -> tuple[str, str | None] | None:
        """Return (phone_number, first_name) or ``None`` if not found."""
        ...


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
class ContextMessage:
    """A safe, UI-oriented message for the read-only context viewer."""

    direction: str
    timestamp: datetime
    text: str | None
    message_type: str


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


def _view_to_item(view: WaitingForMeView, *, now: datetime) -> WaitingListItem:
    """Convert a :class:`WaitingForMeView` to a :class:`WaitingListItem`."""
    waiting_hours = (now - view.waiting_since).total_seconds() / 3600.0
    return WaitingListItem(
        active_id=view.id,
        contact_name=view.contact_name,
        situation_summary=view.summary,
        message_preview=view.last_message,
        waiting_since=view.waiting_since,
        waiting_hours=round(waiting_hours, 1),
        expected_version=view.version,
        is_starred=view.is_starred,
    )


class WaitingListActionService:
    """List waiting items and execute actions for the mini web app.

    Args:
        token_service: The :class:`WaitingListTokenService` for session
            validation.
        query_service: The shared :class:`WaitingListQueryService`. The
            canonical read path (``current_views``) lives here.
        action_service: The :class:`WaitingForMeActionService` for
            executing actions.
        action_repo: The :class:`WaitingForMeActionRepository` for
            per-session summary counts.
        chat_state_repo: The :class:`ChatStateRepository` for name
            resolution fallback in contact creation.
        message_repo: The :class:`MessageRepository` for last message
            text in contact creation fallback.
        contact_repo: The :class:`ContactRepository` for name resolution
            and star/label/tags persistence.
        resolver: The shared :class:`ContactNameResolver` for display
            name resolution in contact creation.
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
        resolver: ContactNameResolver,
        result_repo: WaitingForMeResultRepository | None = None,
        scheduling_service: SchedulingService | None = None,
        active_repo: WaitingForMeActiveRepository | None = None,
        tz_name: str = "Asia/Jerusalem",
        user_info_resolver: UserInfoResolver | None = None,
    ) -> None:
        self._token_service = token_service
        self._query_service = query_service
        self._action_service = action_service
        self._action_repo = action_repo
        self._chat_state_repo = chat_state_repo
        self._message_repo = message_repo
        self._contact_repo = contact_repo
        self._resolver = resolver
        self._result_repo = result_repo
        self._scheduling_service = scheduling_service
        self._active_repo = active_repo
        self._tz_name = tz_name
        self._user_info_resolver = user_info_resolver

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
        views = await self._query_service.current_views(user_id, now=now)
        items = [_view_to_item(v, now=now) for v in views]
        summary = await self._build_summary(session_id, user_id, len(items))
        return WaitingListResponse(items=items, summary=summary)

    async def get_context(
        self,
        session_id: str,
        user_id: str,
        active_id: str,
        limit: int = 8,
    ) -> list[ContextMessage] | None:
        """Return recent messages for an owned waiting item.

        The active row is resolved server-side to prevent the client from
        selecting an arbitrary chat. Returns ``None`` for an invalid session,
        missing item, or item owned by another user.
        """
        resolved = await self._token_service.resolve_session(session_id)
        if resolved is None or resolved.user_id != user_id:
            return None

        active = await self._active_repo.get_by_id(active_id) if self._active_repo else None
        if active is None or active.user_id != user_id:
            return None

        messages = await self._message_repo.list_recent_for_chat(
            user_id=user_id,
            chat_id=active.chat_id,
            limit=max(1, min(limit, 20)),
        )
        return [
            ContextMessage(
                direction=message.direction.value,
                timestamp=message.timestamp,
                text=message.text,
                message_type=message.message_type,
            )
            for message in messages
        ]

    @traceable(name="wfm.miniapp.button_click")
    async def _trace_button_click(self, *, user_id: str, button: str) -> None:
        """Emit a success-only trace for a mini-app button click.

        Called only after APPLIED (execute_action) or "scheduled"
        (schedule_send) outcomes so the LangSmith chart counts successful
        actions, not retries/stale/not_found. Attaches ``button`` and the
        HMAC-hashed ``user_id_hash`` to the run metadata for per-user
        grouping. When a ``user_info_resolver`` is configured, also
        attaches raw ``user_phone`` and ``user_name`` for dashboard
        display.

        Safe when tracing is disabled: ``get_current_run_tree()`` returns
        ``None`` and the block is a no-op; ``correlation_id`` is never
        called (so ``OBSERVABILITY_HASH_KEY`` is not required).
        """
        run_tree = get_current_run_tree()
        if run_tree is None:
            return
        metadata: dict[str, object] = {
            "button": button,
            "user_id_hash": correlation_id(user_id),
        }
        if self._user_info_resolver is not None:
            info = await self._user_info_resolver.get_user_info_by_id(user_id)
            if info is not None:
                phone, name = info
                metadata["user_phone"] = phone
                if name is not None:
                    metadata["user_name"] = name
        run_tree.add_metadata(metadata)

    async def _trace_button_outcome(
        self,
        *,
        user_id: str,
        button: str,
        outcome: str,
    ) -> None:
        """Emit a button-specific trace name for reliable chart filtering.

        Emits two trace names:
        - ``wfm.miniapp.action.{button}`` for successful actions
        - ``wfm.miniapp.action.failed`` for duplicate/stale/not_found/invalid

        This avoids relying on metadata filters (which the chart API
        mishandles) — charts can filter by ``name`` instead.
        """
        trace_name = (
            f"wfm.miniapp.action.{button}"
            if outcome == "applied" or outcome == "scheduled"
            else "wfm.miniapp.action.failed"
        )

        @traceable(name=trace_name)
        async def _emit() -> None:
            run_tree = get_current_run_tree()
            if run_tree is None:
                return
            metadata: dict[str, object] = {
                "button": button,
                "outcome": outcome,
                "user_id_hash": correlation_id(user_id),
            }
            if self._user_info_resolver is not None:
                info = await self._user_info_resolver.get_user_info_by_id(user_id)
                if info is not None:
                    phone, name = info
                    metadata["user_phone"] = phone
                    if name is not None:
                        metadata["user_name"] = name
            run_tree.add_metadata(metadata)

        await _emit()

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
        button: str | None = None,
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
            active = await self._active_repo.get_by_id(active_id) if self._active_repo else None
            if active is not None:
                view = await self._query_service.build_view_for_active(user_id, active)
                now = datetime.now(timezone.utc)
                item = _view_to_item(view, now=now)

        summary = await self._build_summary(session_id, user_id)
        # Emit analytics traces.
        # - wfm.miniapp.button_click: generic success trace (backward compat)
        # - wfm.miniapp.action.{button}: button-specific trace for reliable
        #   chart filtering by name (metadata filters are unreliable)
        # - wfm.miniapp.action.failed: failed attempts (duplicate/stale/etc)
        if button is not None:
            if outcome_str == "applied":
                await self._trace_button_click(user_id=user_id, button=button)
            await self._trace_button_outcome(
                user_id=user_id, button=button, outcome=outcome_str
            )
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
        button: str | None = None,
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

        # Check for duplicate first — the active item may already be
        # resolved from a previous call with the same request_id.
        existing = await self._scheduling_service.get_by_request_id(
            request_id, user_id
        )
        if existing is not None:
            return SendResponse(
                outcome="duplicate",
                scheduled_for=existing.execute_at_utc.isoformat(),
                action_id=existing.id,
            )

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

        # Resolve the active waiting item — scheduling a message means
        # the user has decided how to handle it. Only resolve on first
        # creation (not on duplicate retries), to avoid a stale version
        # error clobbering the scheduled send response.
        if created:
            await self._action_service.done(
                user_id=user_id,
                active_id=active_id,
                target_version=active.target_version,
                provider_message_id=f"send:{request_id}",
                session_id=session_id,
            )
        # Emit analytics traces.
        # - wfm.miniapp.button_click: generic success trace (backward compat)
        # - wfm.miniapp.action.send: button-specific trace for reliable
        #   chart filtering by name
        # - wfm.miniapp.action.failed: duplicate retries
        btn = button or "send"
        outcome_str = "scheduled" if created else "duplicate"
        if created:
            await self._trace_button_click(user_id=user_id, button=btn)
        await self._trace_button_outcome(
            user_id=user_id, button=btn, outcome=outcome_str
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

        assert self._active_repo is not None  # required for star/label/tags
        active = await self._active_repo.get_by_id(active_id)
        if active is None or active.user_id != user_id:
            return StarResponse(outcome="not_found")

        phone = phone_from_chat_id(active.chat_id)

        # Resolve a display name for potential contact creation.
        display_name = await self._resolver.resolve_name(user_id, active.chat_id)
        if not display_name:
            display_name = phone

        await self._contact_repo.set_starred(
            user_id=user_id,
            phone=phone,
            is_starred=is_starred,
            display_name=display_name,
        )
        return StarResponse(outcome="updated", is_starred=is_starred)

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
