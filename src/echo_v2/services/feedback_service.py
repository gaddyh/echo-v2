"""WaitingForMeActionService + WaitingForMeFeedbackService.

Clean separation enforced at the service layer:

* :class:`WaitingForMeActionService` — executes user actions (handled,
  snooze, dismiss). Mutates active state. Uses atomic conditional writes
  (``apply_if_version``) to prevent races.
* :class:`WaitingForMeFeedbackService` — records correctness signals
  (correct, false_positive, uncertain, false_negative). Does NOT mutate
  active state. Links feedback to the immutable ``result_id``.

Both services return :class:`HandlingOutcome` — never bare booleans.

UX model (3-button card):
  טופל       → resolve + CORRECT feedback (identification was right, user handled it)
  להזכיר לי   → snooze (remind me later)
  לא צריד    → opens dismiss submenu:
    לא מחכים לי       → resolve + FALSE_POSITIVE feedback (Echo was wrong)
    לא מעניין (שיחכו) → resolve (no feedback, user chooses not to handle)

Idempotency:
  * Actions: ``UNIQUE(user_id, provider_message_id)`` on
    ``waiting_for_me_actions``. Duplicate callback → ``DUPLICATE``.
  * Feedback: ``UNIQUE(user_id, provider_message_id)`` AND
    ``UNIQUE(user_id, result_id)`` on ``waiting_for_me_feedback``.
    First feedback for a result wins; later attempts → ``DUPLICATE``.

Atomicity (actions):
  The action record and the active-state mutation are in the same
  database transaction. The conditional write
  ``apply_if_version(active_id, user_id, target_version, mutate)``
  checks the version AND mutates in one statement — no race window.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from langsmith import traceable

from echo_v2.domain.feedback import (
    FeedbackVerdict,
    HandlingOutcome,
)
from echo_v2.observability.privacy import correlation_id
from echo_v2.observability.tracing import tracing_client
from echo_v2.persistence.chat_repositories import (
    WaitingForMeActiveRepository,
    WaitingForMeResultRepository,
)
from echo_v2.persistence.feedback_repositories import (
    ChatMuteRepository,
    ChatNotInterestedClickRepository,
    WaitingForMeActionRepository,
    WaitingForMeFeedbackRepository,
)
from echo_v2.services.time_presets import preset_to_utc as _preset_to_utc

__all__ = ["WaitingForMeActionService", "WaitingForMeFeedbackService"]

_logger = logging.getLogger("echo_v2.services.feedback")

# Feedback snapshot retention.
SNAPSHOT_RETENTION_DAYS = 90

# Default digest hour (08:00 local time).
DEFAULT_DIGEST_HOUR = 8
DEFAULT_TZ = "Asia/Jerusalem"

# Escalating chat snooze on "לא מעניין, שיחכו" — click count → mute duration.
# Click 1 → 24h, 2 → 48h, 3 → 1 week, 4+ → permanent mute.
_NOT_INTERESTED_SNOOZE_HOURS = 24
_NOT_INTERESTED_SNOOZE_DAYS_WEEK = 7
_NOT_INTERESTED_PERMANENT_THRESHOLD = 4


def _next_digest_at(
    *,
    now_utc: datetime,
    tz_name: str = DEFAULT_TZ,
    digest_hour: int = DEFAULT_DIGEST_HOUR,
) -> datetime:
    """Compute the next local digest time (08:00) in the user's timezone.

    If local time is before 08:00 today, snooze until 08:00 today.
    Otherwise, snooze until 08:00 tomorrow.
    """
    tz = ZoneInfo(tz_name)
    local_now = now_utc.astimezone(tz)
    target = local_now.replace(hour=digest_hour, minute=0, second=0, microsecond=0)
    if target <= local_now:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


# Max snooze duration from now (7 days).
_MAX_SNOOZE_DAYS = 7


def make_snooze_reminder_validator(
    active_repo: WaitingForMeActiveRepository,
) -> Callable[[dict[str, Any]], Awaitable[bool]]:
    """Build a send_validator for SchedulingService that suppresses stale snooze reminders.

    Before sending a snooze reminder, checks that the active item still
    matches the state when the reminder was scheduled. Returns ``True``
    if the reminder should still fire, ``False`` if it should be skipped.

    This handles:
    * Done/dismissed after snooze (active row deleted).
    * New message changing the chat version (target_version mismatch).
    * Re-snooze to a different time (snoozed_until mismatch).
    """

    async def _validate(payload: dict[str, Any]) -> bool:
        active_id = payload.get("active_id")
        target_version = payload.get("target_version")
        expected_snoozed_until = payload.get("expected_snoozed_until")
        if not active_id or target_version is None or not expected_snoozed_until:
            return True  # can't validate, let it through
        active = await active_repo.get_by_id(active_id)
        if active is None:
            return False  # item was done/dismissed/deleted
        if active.target_version != target_version:
            return False  # version changed (new message)
        if active.snoozed_until is None:
            return False  # no longer snoozed
        return bool(active.snoozed_until.isoformat() == expected_snoozed_until)

    return _validate


class WaitingForMeActionService:
    """Execute user actions on active waiting items.

    All actions use atomic conditional writes — no race window between
    validation and mutation.

    Args:
        active_repo: The :class:`WaitingForMeActiveRepository`.
        action_repo: The :class:`WaitingForMeActionRepository`.
        mute_repo: The :class:`ChatMuteRepository`.
        feedback_repo: Optional :class:`WaitingForMeFeedbackRepository`
            for recording implicit feedback on "handled" and "dismiss".
        click_repo: Optional :class:`ChatNotInterestedClickRepository`
            for the escalating "לא מעניין, שיחכו" chat snooze. When
            ``None``, the counter is not tracked and no mute is applied.
    """

    def __init__(
        self,
        *,
        active_repo: WaitingForMeActiveRepository,
        action_repo: WaitingForMeActionRepository,
        mute_repo: ChatMuteRepository,
        feedback_repo: WaitingForMeFeedbackRepository | None = None,
        result_repo: WaitingForMeResultRepository | None = None,
        scheduling_service: Any | None = None,
        user_phone_lookup: Any | None = None,
        chat_name_lookup: Any | None = None,
        token_service: Any | None = None,
        reminder_template_name: str = "snooze_reminder_v1",
        click_repo: ChatNotInterestedClickRepository | None = None,
    ) -> None:
        self._active_repo = active_repo
        self._action_repo = action_repo
        self._mute_repo = mute_repo
        self._feedback_repo = feedback_repo
        self._result_repo = result_repo
        self._scheduling_service = scheduling_service
        self._user_phone_lookup = user_phone_lookup
        self._chat_name_lookup = chat_name_lookup
        self._token_service = token_service
        self._reminder_template_name = reminder_template_name
        self._click_repo = click_repo

    async def _apply_not_interested_snooze(
        self,
        *,
        user_id: str,
        chat_id: str,
        now: datetime | None = None,
    ) -> None:
        """Increment the per-chat not-interested counter and apply an
        escalating mute: 24h → 48h → 1 week → permanent.

        No-op when ``click_repo`` is not configured.
        """
        if self._click_repo is None:
            return
        now = now or datetime.now(timezone.utc)
        count = await self._click_repo.increment(
            user_id=user_id, chat_id=chat_id, now=now,
        )
        if count >= _NOT_INTERESTED_PERMANENT_THRESHOLD:
            await self._mute_repo.mute_permanent(
                user_id=user_id, chat_id=chat_id,
            )
        elif count == 3:
            await self._mute_repo.mute_temporary(
                user_id=user_id,
                chat_id=chat_id,
                muted_until=now + timedelta(days=_NOT_INTERESTED_SNOOZE_DAYS_WEEK),
            )
        elif count == 2:
            await self._mute_repo.mute_temporary(
                user_id=user_id,
                chat_id=chat_id,
                muted_until=now + timedelta(hours=_NOT_INTERESTED_SNOOZE_HOURS * 2),
            )
        else:  # count == 1
            await self._mute_repo.mute_temporary(
                user_id=user_id,
                chat_id=chat_id,
                muted_until=now + timedelta(hours=_NOT_INTERESTED_SNOOZE_HOURS),
            )
        _logger.info(
            "action: not_interested snooze user=%s chat=%s click_count=%d",
            user_id, chat_id, count,
        )

    async def _reset_not_interested_clicks(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> None:
        """Reset the per-chat not-interested counter (user re-engaged).

        No-op when ``click_repo`` is not configured.
        """
        if self._click_repo is None:
            return
        await self._click_repo.reset(user_id=user_id, chat_id=chat_id)

    @traceable(name="wfm.action.handled")
    async def handled(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        provider_message_id: str,
    ) -> HandlingOutcome:
        """Handle 'טופל' — resolve the active item + record CORRECT feedback.

        The user says they already handled it. The identification was
        correct (someone WAS waiting), so we record a CORRECT feedback
        signal as a side effect.

        Returns APPLIED, DUPLICATE, STALE, or NOT_FOUND.
        """
        result = await self._action_repo.resolve_and_delete_by_chat(
            user_id=user_id,
            active_id=active_id,
            target_version=target_version,
            provider_message_id=provider_message_id,
        )
        if result.outcome is not HandlingOutcome.APPLIED:
            return result.outcome

        # Record implicit CORRECT feedback.
        if self._feedback_repo is not None and result.chat_id is not None:
            await self._feedback_repo.record(
                user_id=user_id,
                chat_id=result.chat_id,
                verdict=FeedbackVerdict.CORRECT,
                result_id=result.result_id,
                target_version=target_version,
                provider_message_id=f"implicit:{provider_message_id}",
            )

        # User re-engaged — reset the not-interested click counter.
        if result.chat_id is not None:
            await self._reset_not_interested_clicks(
                user_id=user_id, chat_id=result.chat_id,
            )

        _logger.info(
            "action: handled user=%s active_id=%s version=%d",
            user_id, active_id, target_version,
        )
        return HandlingOutcome.APPLIED

    @traceable(name="wfm.action.snooze")
    async def snooze(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        provider_message_id: str,
        tz_name: str = DEFAULT_TZ,
        snooze_preset: str | None = None,
        snooze_until: datetime | None = None,
        now_utc: datetime | None = None,
        session_id: str | None = None,
    ) -> HandlingOutcome:
        """Handle 'להזכיר לי' — set snoozed_until.

        Three modes:
        * ``snooze_until`` provided → use it directly (validated: future,
          ≤ 7 days ahead).
        * ``snooze_preset`` provided → map to a local-time target
          (morning/afternoon/evening/tomorrow) using the user's timezone.
        * Neither → default: next local digest hour (08:00).

        Returns APPLIED, DUPLICATE, STALE, or NOT_FOUND.
        """
        now = now_utc or datetime.now(timezone.utc)

        if snooze_until is not None:
            # Validate: must be in the future and ≤ 7 days ahead.
            if snooze_until <= now:
                _logger.warning(
                    "action: snooze_until in the past user=%s active_id=%s",
                    user_id, active_id,
                )
                return HandlingOutcome.INVALID
            if snooze_until > now + timedelta(days=_MAX_SNOOZE_DAYS):
                _logger.warning(
                    "action: snooze_until too far user=%s active_id=%s",
                    user_id, active_id,
                )
                return HandlingOutcome.INVALID
            snoozed_until = snooze_until
        elif snooze_preset is not None:
            try:
                snoozed_until = _preset_to_utc(
                    snooze_preset, now_utc=now, tz_name=tz_name
                )
            except ValueError:
                _logger.warning(
                    "action: invalid snooze preset %s user=%s",
                    snooze_preset, user_id,
                )
                return HandlingOutcome.INVALID
        else:
            # Default: 1 hour from now.
            snoozed_until = now + timedelta(hours=1)

        action_payload: dict[str, Any] = {"snoozed_until": snoozed_until.isoformat()}
        if snooze_preset is not None:
            action_payload["snooze_preset"] = snooze_preset
        if snooze_until is not None:
            action_payload["snooze_custom"] = True
        if session_id is not None:
            action_payload["source"] = "waiting_list_web"
            action_payload["waiting_list_session_id"] = session_id

        result = await self._action_repo.snooze_active(
            user_id=user_id,
            active_id=active_id,
            target_version=target_version,
            snoozed_until=snoozed_until,
            provider_message_id=provider_message_id,
            action_payload=action_payload,
        )
        if result.outcome is not HandlingOutcome.APPLIED:
            if result.outcome is HandlingOutcome.STALE:
                _logger.warning(
                    "action: snooze stale or not found "
                    "user=%s active_id=%s version=%d",
                    user_id, active_id, target_version,
                )
            return result.outcome

        _logger.info(
            "action: snooze user=%s active_id=%s version=%d until=%s",
            user_id, active_id, target_version, snoozed_until,
        )

        # Schedule a WhatsApp reminder via the existing scheduler infra.
        if result.chat_id is not None:
            await self._schedule_snooze_reminder(
                user_id=user_id,
                active_id=active_id,
                chat_id=result.chat_id,
                target_version=target_version,
                snoozed_until=snoozed_until,
            )

        return HandlingOutcome.APPLIED

    async def _schedule_snooze_reminder(
        self,
        *,
        user_id: str,
        active_id: str,
        chat_id: str,
        target_version: int,
        snoozed_until: datetime,
    ) -> None:
        """Schedule a SEND_BOT_MESSAGE for the snooze expiry.

        Uses the existing scheduler infrastructure — no separate worker
        needed. The reminder is sent as a pre-approved WhatsApp template
        (``snooze_reminder_v1``) with a URL button that opens the
        waiting-list mini-app. Templates can be delivered outside the
        24-hour session window, unlike free-text interactive buttons.

        The template body carries the contact display name as ``{{1}}``.
        The URL button carries an opaque waiting-list session token as
        ``{{1}}`` (issued here, resolved when the user opens the link).

        Failures to schedule are logged but do not fail the snooze action.
        """
        if self._scheduling_service is None or self._user_phone_lookup is None:
            return

        try:
            phone = await self._user_phone_lookup(user_id)
            if phone is None:
                _logger.warning(
                    "snooze: no phone for user %s, skipping reminder",
                    user_id,
                )
                return

            # Resolve display name.
            name = None
            if self._chat_name_lookup is not None:
                name = await self._chat_name_lookup(user_id, chat_id)
            display_name = name or "לקוח"

            # Issue a waiting-list web session token for the URL button.
            url_suffix: str | None = None
            if self._token_service is not None:
                try:
                    _session_id, raw_token = await self._token_service.issue(user_id)
                    url_suffix = raw_token
                except Exception:
                    _logger.exception(
                        "snooze: failed to issue waiting-list token for "
                        "user %s, sending reminder without URL button",
                        user_id,
                    )

            from echo_v2.domain.scheduling import ScheduledActionType

            await self._scheduling_service.create(
                user_id=user_id,
                type=ScheduledActionType.SEND_BOT_MESSAGE,
                execute_at_utc=snoozed_until,
                timezone_name="UTC",
                payload={
                    "kind": "waiting_for_me_reminder",
                    "active_id": active_id,
                    "target_version": target_version,
                    "expected_snoozed_until": snoozed_until.isoformat(),
                    "chat_id": phone,
                    "template": {
                        "name": self._reminder_template_name,
                        "language": "he",
                        "body_params": [display_name],
                        "url_suffix": url_suffix,
                    },
                },
            )
            _logger.info(
                "snooze: scheduled reminder for user %s at %s",
                user_id,
                snoozed_until.isoformat(),
            )
        except Exception:
            _logger.exception(
                "snooze: failed to schedule reminder for user %s, "
                "snooze still applied",
                user_id,
            )

    @traceable(name="wfm.action.dismiss_not_waiting")
    async def dismiss_not_waiting(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        provider_message_id: str,
    ) -> HandlingOutcome:
        """Handle 'לא מחכים לי' — resolve + record FALSE_POSITIVE feedback.

        The user says Echo was wrong — nobody is actually waiting.
        Records a FALSE_POSITIVE feedback signal for training.

        Returns APPLIED, DUPLICATE, STALE, or NOT_FOUND.
        """
        result = await self._action_repo.resolve_and_delete_by_chat(
            user_id=user_id,
            active_id=active_id,
            target_version=target_version,
            action_payload={"dismiss_reason": "not_waiting"},
            provider_message_id=provider_message_id,
        )
        if result.outcome is not HandlingOutcome.APPLIED:
            if result.outcome is HandlingOutcome.STALE:
                _logger.warning(
                    "action: dismiss_not_waiting stale "
                    "user=%s active_id=%s version=%d",
                    user_id, active_id, target_version,
                )
            return result.outcome

        # Record implicit FALSE_POSITIVE feedback.
        if self._feedback_repo is not None and result.chat_id is not None:
            await self._feedback_repo.record(
                user_id=user_id,
                chat_id=result.chat_id,
                verdict=FeedbackVerdict.FALSE_POSITIVE,
                result_id=result.result_id,
                target_version=target_version,
                provider_message_id=f"implicit:{provider_message_id}",
            )

        # Fire-and-forget: enqueue the conversation + analyzer decision to a
        # dedicated LangSmith annotation queue for human review. The DB
        # feedback above is the durable source of truth; this is a best-effort
        # projection. Non-blocking — the user's dismiss returns immediately.
        self._schedule_false_positive_annotation(
            result_id=result.result_id, user_id=user_id,
        )

        _logger.info(
            "action: dismiss_not_waiting user=%s active_id=%s version=%d",
            user_id, active_id, target_version,
        )
        return HandlingOutcome.APPLIED

    @traceable(name="wfm.action.dismiss_not_interested")
    async def dismiss_not_interested(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        provider_message_id: str,
    ) -> HandlingOutcome:
        """Handle 'לא מעניין (שיחכו)' — resolve without feedback.

        The user acknowledges someone may be waiting but chooses not to
        handle it. No feedback signal — this is a user choice, not a
        judgment on Echo's accuracy.

        Returns APPLIED, DUPLICATE, STALE, or NOT_FOUND.
        """
        result = await self._action_repo.resolve_and_delete_by_chat(
            user_id=user_id,
            active_id=active_id,
            target_version=target_version,
            action_payload={"dismiss_reason": "not_interested"},
            provider_message_id=provider_message_id,
        )
        if result.outcome is not HandlingOutcome.APPLIED:
            if result.outcome is HandlingOutcome.STALE:
                _logger.warning(
                    "action: dismiss_not_interested stale "
                    "user=%s active_id=%s version=%d",
                    user_id, active_id, target_version,
                )
            return result.outcome

        # Apply the escalating chat snooze (24h → 48h → 1 week → permanent).
        if result.chat_id is not None:
            await self._apply_not_interested_snooze(
                user_id=user_id, chat_id=result.chat_id,
            )

        _logger.info(
            "action: dismiss_not_interested user=%s active_id=%s version=%d",
            user_id, active_id, target_version,
        )
        return HandlingOutcome.APPLIED

    @traceable(name="wfm.action.done")
    async def done(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        provider_message_id: str,
        session_id: str | None = None,
    ) -> HandlingOutcome:
        """Handle 'בוצע' from the web app — resolve the active item.

        Operational action only — does NOT record CORRECT feedback.
        "Done" means the owner completed something; it does not prove
        the detector's classification was correct.

        Uses atomic version-checked delete (``delete_if_version``).
        Idempotency is checked before item existence via the action
        record's unique constraint on ``provider_message_id``.

        Returns APPLIED, DUPLICATE, STALE, or NOT_FOUND.
        """
        action_payload: dict[str, Any] = {"source": "waiting_list_web"}
        if session_id is not None:
            action_payload["waiting_list_session_id"] = session_id

        result = await self._action_repo.resolve_and_delete_by_version(
            user_id=user_id,
            active_id=active_id,
            target_version=target_version,
            action_payload=action_payload,
            provider_message_id=provider_message_id,
        )
        if result.outcome is HandlingOutcome.APPLIED:
            # User re-engaged — reset the not-interested click counter.
            if result.chat_id is not None:
                await self._reset_not_interested_clicks(
                    user_id=user_id, chat_id=result.chat_id,
                )
            _logger.info(
                "action: done user=%s active_id=%s version=%d",
                user_id, active_id, target_version,
            )
            return HandlingOutcome.APPLIED
        if result.outcome is HandlingOutcome.STALE:
            _logger.warning(
                "action: done stale user=%s active_id=%s version=%d",
                user_id, active_id, target_version,
            )
        return result.outcome

    @traceable(name="wfm.action.dismiss_with_reason")
    async def dismiss_with_reason(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        provider_message_id: str,
        reason: str,
        session_id: str | None = None,
    ) -> HandlingOutcome:
        """Handle 'לא נדרש' from the web app — resolve with a reason.

        Reasons:
        * ``already_handled`` → resolve, no feedback.
        * ``no_response_required`` → resolve, no feedback.
        * ``detected_incorrectly`` → resolve + FALSE_POSITIVE feedback.

        Uses atomic version-checked delete (``delete_if_version``).
        Idempotency is checked before item existence via the action
        record's unique constraint on ``provider_message_id``.

        Returns APPLIED, DUPLICATE, STALE, or NOT_FOUND.
        """
        action_payload: dict[str, Any] = {
            "source": "waiting_list_web",
            "dismiss_reason": reason,
        }
        if session_id is not None:
            action_payload["waiting_list_session_id"] = session_id

        result = await self._action_repo.resolve_and_delete_by_version(
            user_id=user_id,
            active_id=active_id,
            target_version=target_version,
            action_payload=action_payload,
            provider_message_id=provider_message_id,
        )
        if result.outcome is not HandlingOutcome.APPLIED:
            if result.outcome is HandlingOutcome.STALE:
                _logger.warning(
                    "action: dismiss_with_reason stale "
                    "user=%s active_id=%s version=%d reason=%s",
                    user_id, active_id, target_version, reason,
                )
            return result.outcome

        # Record FALSE_POSITIVE feedback only for "detected_incorrectly".
        if (
            reason == "detected_incorrectly"
            and self._feedback_repo is not None
            and result.chat_id is not None
        ):
            await self._feedback_repo.record(
                user_id=user_id,
                chat_id=result.chat_id,
                verdict=FeedbackVerdict.FALSE_POSITIVE,
                result_id=result.result_id,
                target_version=target_version,
                provider_message_id=f"implicit:{provider_message_id}",
            )
            # Fire-and-forget: enqueue to the user false-positive annotation
            # queue (same signal as the WhatsApp "לא מחכים לי" path).
            self._schedule_false_positive_annotation(
                result_id=result.result_id, user_id=user_id,
            )

        # Escalating chat snooze for "לא מעניין, שיחכו" (web path).
        if reason == "no_response_required" and result.chat_id is not None:
            await self._apply_not_interested_snooze(
                user_id=user_id, chat_id=result.chat_id,
            )

        _logger.info(
            "action: dismiss_with_reason user=%s active_id=%s version=%d reason=%s",
            user_id, active_id, target_version, reason,
        )
        return HandlingOutcome.APPLIED

    async def mute_chat(
        self,
        *,
        user_id: str,
        chat_id: str,
        permanent: bool,
        provider_message_id: str,
    ) -> HandlingOutcome:
        """Mute a chat (temporary or permanent). This is an action, not feedback."""
        muted_until = (
            None if permanent
            else datetime.now(timezone.utc) + timedelta(hours=48)
        )
        result = await self._action_repo.mute_chat_atomic(
            user_id=user_id,
            chat_id=chat_id,
            permanent=permanent,
            muted_until=muted_until,
            provider_message_id=provider_message_id,
        )
        if result.outcome is not HandlingOutcome.APPLIED:
            return result.outcome
        _logger.info(
            "action: mute_chat user=%s chat=%s permanent=%s",
            user_id, chat_id, permanent,
        )
        return HandlingOutcome.APPLIED

    async def unmute_chat(
        self,
        *,
        user_id: str,
        chat_id: str,
        provider_message_id: str,
    ) -> HandlingOutcome:
        """Unmute a chat."""
        result = await self._action_repo.unmute_chat_atomic(
            user_id=user_id,
            chat_id=chat_id,
            provider_message_id=provider_message_id,
        )
        if result.outcome is not HandlingOutcome.APPLIED:
            return result.outcome
        _logger.info("action: unmute_chat user=%s chat=%s", user_id, chat_id)
        return HandlingOutcome.APPLIED

    # --- User false-positive annotation flywheel -----------------------------

    def _schedule_false_positive_annotation(
        self,
        *,
        result_id: str | None,
        user_id: str,
    ) -> None:
        """Fire-and-forget: enqueue a user false-positive to the annotation queue.

        Called from both false-positive paths (``dismiss_not_waiting`` and
        ``dismiss_with_reason("detected_incorrectly")``) after the durable
        DB feedback is persisted. The annotation queue is a best-effort
        projection for human review — never blocks the user's action.
        """
        if result_id is None:
            return
        asyncio.create_task(
            self._enqueue_user_annotation(
                result_id=result_id,
                user_id=user_id,
            )
        )

    @traceable(
        name="wfm.user_false_positive",
        client=tracing_client,
    )
    async def _trace_user_false_positive(
        self,
        *,
        conversation: list[dict[str, Any]],
        analyzer_decision: str,
        next_owner: str | None,
        open_obligation: str | None,
        analyzer_reason: str | None,
        analyzer_confidence: float | None,
        model: str | None,
        prompt_version: str | None,
        analyzer_version: str | None,
        target_version: int,
        user_id_hash: str,
    ) -> tuple[str | None, str | None]:
        """Create a self-contained trace for a user-reported false positive.

        The kwargs become the run's inputs in LangSmith — visible to the
        annotator and filterable by ``prompt_version`` / ``model`` /
        ``next_owner``. ``user_id`` is never passed raw; only the HMAC
        hash (``user_id_hash``) is attached via run metadata.

        Returns ``(run_id, run_start_time)`` or ``(None, None)`` when
        tracing is disabled (``get_current_run_tree()`` is None).
        """
        from langsmith.run_helpers import get_current_run_tree

        run_tree = get_current_run_tree()
        if run_tree is None:
            return None, None
        run_tree.add_metadata({
            "user_id_hash": user_id_hash,
            "feedback_source": "user_false_positive",
        })
        return (
            str(run_tree.id),
            run_tree.start_time.isoformat(),
        )

    async def _enqueue_user_annotation(
        self,
        *,
        result_id: str,
        user_id: str,
    ) -> None:
        """Load the immutable result, trace it, and enqueue to the annotation queue.

        Best-effort: any failure (network, missing queue, missing result)
        logs a warning and returns — never propagates to the caller. The
        DB ``waiting_for_me_feedback`` row is the durable source of truth.
        """
        queue_id = os.environ.get("USER_ANNOTATION_QUEUE_ID", "")
        if not queue_id or self._result_repo is None:
            return
        try:
            result = await self._result_repo.get_by_id(result_id)
            if result is None or not result.conversation_snapshot:
                return
            messages = result.conversation_snapshot.get("messages")
            if not messages:
                return

            run_id, run_start_time = await self._trace_user_false_positive(
                conversation=messages,
                analyzer_decision=result.decision.value,
                next_owner=(
                    result.next_owner.value
                    if result.next_owner is not None
                    else None
                ),
                open_obligation=result.open_obligation,
                analyzer_reason=result.reason,
                analyzer_confidence=result.confidence,
                model=result.model,
                prompt_version=result.prompt_version,
                analyzer_version=result.analyzer_version,
                target_version=result.target_version,
                user_id_hash=correlation_id(user_id),
            )

            if run_id is None or run_start_time is None:
                return

            # Flush so the run is persisted before the queue references it.
            tracing_client.flush()
            session_id = os.environ.get("LANGSMITH_PROJECT_ID", "")
            # LangSmith's indexing is asynchronous — the run may not be
            # queryable immediately after flush. Retry with backoff.
            await self._enqueue_with_retry(
                queue_id=queue_id,
                run_id=run_id,
                session_id=session_id,
                run_start_time=run_start_time,
                result_id=result_id,
            )
        except Exception:
            _logger.warning(
                "failed to enqueue user false-positive annotation "
                "result_id=%s",
                result_id,
                exc_info=True,
            )

    async def _enqueue_with_retry(
        self,
        *,
        queue_id: str,
        run_id: str,
        session_id: str,
        run_start_time: str,
        result_id: str,
        max_wait: float = 60.0,
        poll_interval: float = 3.0,
        _run_checker: Any = None,
    ) -> None:
        """Poll LangSmith until the run is queryable, then enqueue it.

        LangSmith's run indexing is asynchronous — ``flush()`` returns
        before the run is queryable by the annotation queue API. We poll
        until the run appears, then use the server-side ``start_time``
        and ``session_id`` from the polled run (which may differ from
        the local run_tree values) to enqueue it.

        Args:
            _run_checker: Optional callable ``(run_id) -> dict | None``
                for testing. Returns run info (with ``start_time`` and
                ``session_id``) if queryable, ``None`` otherwise. Defaults
                to querying LangSmith via ``list_runs``.
        """
        import asyncio as _asyncio

        if _run_checker is None:
            from langsmith import Client

            client = Client(
                api_key=os.environ.get("LANGSMITH_API_KEY", ""),
                hide_inputs=False,
                hide_outputs=False,
            )
            project = os.environ.get("LANGSMITH_PROJECT", "")

            def _default_checker(rid: str) -> dict[str, Any] | None:
                try:
                    runs = list(client.list_runs(project_name=project, run_id=rid))
                    if runs:
                        r = runs[0]
                        return {
                            "start_time": r.start_time.isoformat(),
                            "session_id": str(r.session_id),
                        }
                except Exception:  # noqa: BLE001 - poll, transient ok
                    return None
                return None

            _run_checker = _default_checker

        # Poll until the run is queryable.
        elapsed = 0.0
        run_info: dict[str, Any] | None = None
        while elapsed < max_wait:
            run_info = _run_checker(run_id)
            if run_info is not None:
                break
            await _asyncio.sleep(poll_interval)
            elapsed += poll_interval
        else:
            _logger.warning(
                "user false-positive: run %s not queryable after %.0fs, "
                "giving up (result_id=%s)",
                run_id, max_wait, result_id,
            )
            return

        # Use server-side start_time and session_id from the polled run.
        enqueue_start_time = run_info.get("start_time", run_start_time)
        enqueue_session_id = run_info.get("session_id", session_id)

        # Run is queryable — enqueue it.
        await tracing_client.annotation_queues.items.create(
            queue_id=queue_id,
            items=[
                {
                    "item_type": "RUN",
                    "run_id": run_id,
                    "session_id": enqueue_session_id,
                    "start_time": enqueue_start_time,
                }
            ],
        )
        _logger.info(
            "user false-positive: enqueued run %s to annotation queue %s "
            "(result_id=%s, waited %.1fs)",
            run_id, queue_id, result_id, elapsed,
        )


class WaitingForMeFeedbackService:
    """Record correctness signals on analysis results.

    Feedback is linked to the immutable ``result_id`` — the exact analysis
    the user is judging. Does NOT mutate active state.

    In the 3-button UX, feedback is mostly implicit (recorded as a side
    effect of "טופל" and "לא מחכים לי"). This service is still available
    for explicit feedback if needed in the future.

    Args:
        feedback_repo: The :class:`WaitingForMeFeedbackRepository`.
        result_repo: The :class:`WaitingForMeResultRepository` for
            validating that the result exists and belongs to the user.
    """

    def __init__(
        self,
        *,
        feedback_repo: WaitingForMeFeedbackRepository,
        result_repo: WaitingForMeResultRepository,
    ) -> None:
        self._feedback_repo = feedback_repo
        self._result_repo = result_repo

    async def record(
        self,
        *,
        user_id: str,
        result_id: str,
        verdict: FeedbackVerdict,
        provider_message_id: str,
    ) -> HandlingOutcome:
        """Record a feedback verdict for a specific analysis result.

        Returns APPLIED, DUPLICATE, NOT_FOUND, or INVALID.
        """
        expires_at = datetime.now(timezone.utc) + timedelta(days=SNAPSHOT_RETENTION_DAYS)
        feedback = await self._feedback_repo.record(
            user_id=user_id,
            chat_id="",
            verdict=verdict,
            result_id=result_id,
            provider_message_id=provider_message_id,
            expires_at=expires_at,
        )
        if feedback is None:
            return HandlingOutcome.DUPLICATE

        _logger.info(
            "feedback: verdict=%s user=%s result_id=%s",
            verdict.value, user_id, result_id,
        )
        return HandlingOutcome.APPLIED
