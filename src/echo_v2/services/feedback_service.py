"""FeedbackService — handles user actions and feedback on waiting items.

Clean separation:

* **Actions** mutate active state (acknowledge, snooze, resolve, mute).
* **Feedback** records correctness signals (correct, false_positive,
  false_negative) without mutating state.

The service validates every callback:

1. **Ownership** — the active item must belong to the authenticated user.
2. **Version** — ``active.target_version`` must match the callback's
   ``target_version``. Stale callbacks are rejected.
3. **Idempotency** — ``provider_event_id`` prevents duplicate actions
   from webhook retries.

After ``resolve``, the service asks whether Echo was wrong. If yes,
it records ``false_positive`` feedback. If no, it records ``correct``.

After 3+ recent ``false_positive`` verdicts on the same chat (30-day
window), the service offers a permanent mute.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from echo_v2.domain.feedback import (
    FeedbackVerdict,
    WaitingForMeActionType,
)
from echo_v2.persistence.chat_repositories import WaitingForMeActiveRepository
from echo_v2.persistence.feedback_repositories import (
    ChatMuteRepository,
    WaitingForMeActionRepository,
    WaitingForMeFeedbackRepository,
)

__all__ = ["FeedbackService"]

_logger = logging.getLogger("echo_v2.services.feedback")

# After this many recent false_positive verdicts, offer permanent mute.
MUTE_THRESHOLD = 3
MUTE_WINDOW_DAYS = 30

# Snooze duration: until next morning (default 18 hours from now).
SNOOZE_HOURS = 18

# Feedback snapshot retention.
SNAPSHOT_RETENTION_DAYS = 90


class FeedbackService:
    """Handle user actions and feedback on waiting-for-me items.

    Args:
        active_repo: The :class:`WaitingForMeActiveRepository`.
        action_repo: The :class:`WaitingForMeActionRepository`.
        feedback_repo: The :class:`WaitingForMeFeedbackRepository`.
        mute_repo: The :class:`ChatMuteRepository`.
    """

    def __init__(
        self,
        *,
        active_repo: WaitingForMeActiveRepository,
        action_repo: WaitingForMeActionRepository,
        feedback_repo: WaitingForMeFeedbackRepository,
        mute_repo: ChatMuteRepository,
    ) -> None:
        self._active_repo = active_repo
        self._action_repo = action_repo
        self._feedback_repo = feedback_repo
        self._mute_repo = mute_repo

    async def handle_acknowledge(
        self,
        *,
        user_id: str,
        chat_id: str,
        active_id: str,
        target_version: int,
        provider_event_id: str,
    ) -> bool:
        """Handle 'מטפל עכשיו' — set acknowledged_at.

        Returns ``True`` if the action was applied, ``False`` if stale
        or not found.
        """
        if not await self._validate(user_id, chat_id, active_id, target_version):
            return False

        now = datetime.now(timezone.utc)
        action = await self._action_repo.record(
            user_id=user_id,
            chat_id=chat_id,
            action_type=WaitingForMeActionType.ACKNOWLEDGE,
            active_id=active_id,
            target_version=target_version,
            provider_event_id=provider_event_id,
        )
        if action is None:
            # Duplicate — already processed.
            return True

        await self._active_repo.acknowledge(
            user_id=user_id,
            chat_id=chat_id,
            acknowledged_at=now,
        )
        _logger.info(
            "feedback: acknowledge user=%s chat=%s version=%d",
            user_id, chat_id, target_version,
        )
        return True

    async def handle_snooze(
        self,
        *,
        user_id: str,
        chat_id: str,
        active_id: str,
        target_version: int,
        provider_event_id: str,
    ) -> bool:
        """Handle 'הזכר לי מחר' — set snoozed_until.

        Returns ``True`` if the action was applied, ``False`` if stale
        or not found.
        """
        if not await self._validate(user_id, chat_id, active_id, target_version):
            return False

        now = datetime.now(timezone.utc)
        snoozed_until = now + timedelta(hours=SNOOZE_HOURS)
        action = await self._action_repo.record(
            user_id=user_id,
            chat_id=chat_id,
            action_type=WaitingForMeActionType.SNOOZE,
            active_id=active_id,
            target_version=target_version,
            action_payload={"snoozed_until": snoozed_until.isoformat()},
            provider_event_id=provider_event_id,
        )
        if action is None:
            return True

        await self._active_repo.snooze(
            user_id=user_id,
            chat_id=chat_id,
            snoozed_until=snoozed_until,
        )
        _logger.info(
            "feedback: snooze user=%s chat=%s version=%d until=%s",
            user_id, chat_id, target_version, snoozed_until,
        )
        return True

    async def handle_resolve(
        self,
        *,
        user_id: str,
        chat_id: str,
        active_id: str,
        target_version: int,
        provider_event_id: str,
    ) -> bool:
        """Handle 'הסר מהרשימה' — remove from active list.

        Returns ``True`` if the action was applied, ``False`` if stale
        or not found.
        """
        if not await self._validate(user_id, chat_id, active_id, target_version):
            return False

        action = await self._action_repo.record(
            user_id=user_id,
            chat_id=chat_id,
            action_type=WaitingForMeActionType.RESOLVE,
            active_id=active_id,
            target_version=target_version,
            provider_event_id=provider_event_id,
        )
        if action is None:
            return True

        await self._active_repo.delete(user_id=user_id, chat_id=chat_id)
        _logger.info(
            "feedback: resolve user=%s chat=%s version=%d",
            user_id, chat_id, target_version,
        )
        return True

    async def record_feedback(
        self,
        *,
        user_id: str,
        chat_id: str,
        verdict: FeedbackVerdict,
        result_id: str | None = None,
        target_version: int | None = None,
        conversation_snapshot: dict | None = None,
        provider_event_id: str | None = None,
    ) -> bool:
        """Record a correctness signal. Does NOT mutate active state.

        Returns ``True`` if recorded, ``False`` if duplicate.
        """
        expires_at = datetime.now(timezone.utc) + timedelta(days=SNAPSHOT_RETENTION_DAYS)
        feedback = await self._feedback_repo.record(
            user_id=user_id,
            chat_id=chat_id,
            verdict=verdict,
            result_id=result_id,
            target_version=target_version,
            conversation_snapshot=conversation_snapshot,
            provider_event_id=provider_event_id,
            expires_at=expires_at,
        )
        if feedback is not None:
            _logger.info(
                "feedback: verdict=%s user=%s chat=%s",
                verdict.value, user_id, chat_id,
            )
            return True
        return False

    async def should_offer_mute(
        self,
        *,
        user_id: str,
        chat_id: str,
    ) -> bool:
        """Check if we should offer a permanent mute after repeated dismissals.

        Returns ``True`` if there are >= ``MUTE_THRESHOLD`` recent
        ``false_positive`` verdicts in the last ``MUTE_WINDOW_DAYS``.
        """
        since = datetime.now(timezone.utc) - timedelta(days=MUTE_WINDOW_DAYS)
        count = await self._feedback_repo.count_recent_false_positives(
            user_id=user_id,
            chat_id=chat_id,
            since=since,
        )
        return count >= MUTE_THRESHOLD

    async def handle_mute_chat(
        self,
        *,
        user_id: str,
        chat_id: str,
        permanent: bool,
        provider_event_id: str,
    ) -> None:
        """Mute a chat (temporary or permanent)."""
        await self._action_repo.record(
            user_id=user_id,
            chat_id=chat_id,
            action_type=WaitingForMeActionType.MUTE_CHAT,
            provider_event_id=provider_event_id,
        )
        if permanent:
            await self._mute_repo.mute_permanent(user_id=user_id, chat_id=chat_id)
        else:
            await self._mute_repo.mute_temporary(
                user_id=user_id,
                chat_id=chat_id,
                muted_until=datetime.now(timezone.utc) + timedelta(hours=48),
            )
        _logger.info(
            "feedback: mute_chat user=%s chat=%s permanent=%s",
            user_id, chat_id, permanent,
        )

    async def handle_unmute_chat(
        self,
        *,
        user_id: str,
        chat_id: str,
        provider_event_id: str,
    ) -> None:
        """Unmute a chat."""
        await self._action_repo.record(
            user_id=user_id,
            chat_id=chat_id,
            action_type=WaitingForMeActionType.UNMUTE_CHAT,
            provider_event_id=provider_event_id,
        )
        await self._mute_repo.unmute(user_id=user_id, chat_id=chat_id)
        _logger.info("feedback: unmute_chat user=%s chat=%s", user_id, chat_id)

    async def _validate(
        self,
        user_id: str,
        chat_id: str,
        active_id: str,
        target_version: int,
    ) -> bool:
        """Validate ownership + version. Returns ``True`` if valid."""
        active = await self._active_repo.get(user_id=user_id, chat_id=chat_id)
        if active is None:
            _logger.warning(
                "feedback: stale callback — active not found "
                "user=%s chat=%s active_id=%s",
                user_id, chat_id, active_id,
            )
            return False
        if active.target_version != target_version:
            _logger.warning(
                "feedback: stale callback — version mismatch "
                "user=%s chat=%s active=%d callback=%d",
                user_id, chat_id, active.target_version, target_version,
            )
            return False
        return True
