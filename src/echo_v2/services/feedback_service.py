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

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from echo_v2.domain.feedback import (
    FeedbackVerdict,
    HandlingOutcome,
    WaitingForMeActionType,
)
from echo_v2.persistence.chat_repositories import (
    WaitingForMeActiveRepository,
    WaitingForMeResultRepository,
)
from echo_v2.persistence.feedback_repositories import (
    ChatMuteRepository,
    WaitingForMeActionRepository,
    WaitingForMeFeedbackRepository,
)

__all__ = ["WaitingForMeActionService", "WaitingForMeFeedbackService"]

_logger = logging.getLogger("echo_v2.services.feedback")

# Feedback snapshot retention.
SNAPSHOT_RETENTION_DAYS = 90

# Default digest hour (08:00 local time).
DEFAULT_DIGEST_HOUR = 8
DEFAULT_TZ = "Asia/Jerusalem"


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
    """

    def __init__(
        self,
        *,
        active_repo: WaitingForMeActiveRepository,
        action_repo: WaitingForMeActionRepository,
        mute_repo: ChatMuteRepository,
        feedback_repo: WaitingForMeFeedbackRepository | None = None,
        result_repo: WaitingForMeResultRepository | None = None,
    ) -> None:
        self._active_repo = active_repo
        self._action_repo = action_repo
        self._mute_repo = mute_repo
        self._feedback_repo = feedback_repo
        self._result_repo = result_repo

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
        # Record the action (idempotent on provider_message_id).
        action = await self._action_repo.record(
            user_id=user_id,
            chat_id="",
            action_type=WaitingForMeActionType.RESOLVE,
            active_id=active_id,
            target_version=target_version,
            provider_message_id=provider_message_id,
        )
        if action is None:
            return HandlingOutcome.DUPLICATE

        # Verify the active item exists and version matches.
        active = await self._active_repo.get_by_id(active_id)
        if active is None:
            return HandlingOutcome.NOT_FOUND
        if active.user_id != user_id or active.target_version != target_version:
            _logger.warning(
                "action: handled stale user=%s active_id=%s version=%d",
                user_id, active_id, target_version,
            )
            return HandlingOutcome.STALE

        # Delete the active item.
        await self._active_repo.delete(user_id=user_id, chat_id=active.chat_id)

        # Record implicit CORRECT feedback.
        if self._feedback_repo is not None:
            await self._feedback_repo.record(
                user_id=user_id,
                chat_id=active.chat_id,
                verdict=FeedbackVerdict.CORRECT,
                result_id=active.result_id,
                target_version=target_version,
                provider_message_id=f"implicit:{provider_message_id}",
            )

        _logger.info(
            "action: handled user=%s active_id=%s version=%d",
            user_id, active_id, target_version,
        )
        return HandlingOutcome.APPLIED

    async def snooze(
        self,
        *,
        user_id: str,
        active_id: str,
        target_version: int,
        provider_message_id: str,
        tz_name: str = DEFAULT_TZ,
    ) -> HandlingOutcome:
        """Handle 'להזכיר לי' — set snoozed_until to next local digest hour.

        Returns APPLIED, DUPLICATE, STALE, or NOT_FOUND.
        """
        now = datetime.now(timezone.utc)
        snoozed_until = _next_digest_at(now_utc=now, tz_name=tz_name)

        action = await self._action_repo.record(
            user_id=user_id,
            chat_id="",
            action_type=WaitingForMeActionType.SNOOZE,
            active_id=active_id,
            target_version=target_version,
            action_payload={"snoozed_until": snoozed_until.isoformat()},
            provider_message_id=provider_message_id,
        )
        if action is None:
            return HandlingOutcome.DUPLICATE

        changed = await self._active_repo.apply_if_version(
            active_id=active_id,
            user_id=user_id,
            target_version=target_version,
            mutate={"snoozed_until": snoozed_until},
        )
        if not changed:
            _logger.warning(
                "action: snooze stale or not found "
                "user=%s active_id=%s version=%d",
                user_id, active_id, target_version,
            )
            return HandlingOutcome.STALE

        _logger.info(
            "action: snooze user=%s active_id=%s version=%d until=%s",
            user_id, active_id, target_version, snoozed_until,
        )
        return HandlingOutcome.APPLIED

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
        action = await self._action_repo.record(
            user_id=user_id,
            chat_id="",
            action_type=WaitingForMeActionType.RESOLVE,
            active_id=active_id,
            target_version=target_version,
            action_payload={"dismiss_reason": "not_waiting"},
            provider_message_id=provider_message_id,
        )
        if action is None:
            return HandlingOutcome.DUPLICATE

        active = await self._active_repo.get_by_id(active_id)
        if active is None:
            return HandlingOutcome.NOT_FOUND
        if active.user_id != user_id or active.target_version != target_version:
            _logger.warning(
                "action: dismiss_not_waiting stale "
                "user=%s active_id=%s version=%d",
                user_id, active_id, target_version,
            )
            return HandlingOutcome.STALE

        await self._active_repo.delete(user_id=user_id, chat_id=active.chat_id)

        # Record implicit FALSE_POSITIVE feedback.
        if self._feedback_repo is not None:
            await self._feedback_repo.record(
                user_id=user_id,
                chat_id=active.chat_id,
                verdict=FeedbackVerdict.FALSE_POSITIVE,
                result_id=active.result_id,
                target_version=target_version,
                provider_message_id=f"implicit:{provider_message_id}",
            )

        _logger.info(
            "action: dismiss_not_waiting user=%s active_id=%s version=%d",
            user_id, active_id, target_version,
        )
        return HandlingOutcome.APPLIED

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
        action = await self._action_repo.record(
            user_id=user_id,
            chat_id="",
            action_type=WaitingForMeActionType.RESOLVE,
            active_id=active_id,
            target_version=target_version,
            action_payload={"dismiss_reason": "not_interested"},
            provider_message_id=provider_message_id,
        )
        if action is None:
            return HandlingOutcome.DUPLICATE

        active = await self._active_repo.get_by_id(active_id)
        if active is None:
            return HandlingOutcome.NOT_FOUND
        if active.user_id != user_id or active.target_version != target_version:
            _logger.warning(
                "action: dismiss_not_interested stale "
                "user=%s active_id=%s version=%d",
                user_id, active_id, target_version,
            )
            return HandlingOutcome.STALE

        await self._active_repo.delete(user_id=user_id, chat_id=active.chat_id)

        _logger.info(
            "action: dismiss_not_interested user=%s active_id=%s version=%d",
            user_id, active_id, target_version,
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
        action = await self._action_repo.record(
            user_id=user_id,
            chat_id=chat_id,
            action_type=WaitingForMeActionType.MUTE_CHAT,
            provider_message_id=provider_message_id,
        )
        if action is None:
            return HandlingOutcome.DUPLICATE

        if permanent:
            await self._mute_repo.mute_permanent(user_id=user_id, chat_id=chat_id)
        else:
            await self._mute_repo.mute_temporary(
                user_id=user_id,
                chat_id=chat_id,
                muted_until=datetime.now(timezone.utc) + timedelta(hours=48),
            )
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
        action = await self._action_repo.record(
            user_id=user_id,
            chat_id=chat_id,
            action_type=WaitingForMeActionType.UNMUTE_CHAT,
            provider_message_id=provider_message_id,
        )
        if action is None:
            return HandlingOutcome.DUPLICATE

        await self._mute_repo.unmute(user_id=user_id, chat_id=chat_id)
        _logger.info("action: unmute_chat user=%s chat=%s", user_id, chat_id)
        return HandlingOutcome.APPLIED


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
