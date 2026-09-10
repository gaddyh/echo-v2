"""DigestWorker — polls users and sends morning digests.

Runs as a background task (like ChatAnalysisWorker). On each poll:

1. For each active user:
   a. Compute the user's local time from their timezone.
   b. Check if local time is within the digest window (default 08:00–11:00).
   c. If yes, try to claim a daily_digest row for (user_id, local_date).
   d. If claimed (new row): query active waiting chats, build digest, send.
   e. If not claimed (row exists): skip — already processed today.

The active query joins ``waiting_for_me_active`` with ``chats`` to ensure
only current (non-stale) active states are included.

Send errors:
  - IndeterminateError → status = indeterminate (no blind retry)
  - PermanentError → status = failed
  - Other exceptions → status = failed
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from echo_v2.domain.digest import DailyDigestStatus
from echo_v2.domain.waiting_for_me import WaitingForMeActive
from echo_v2.persistence.chat_repositories import (
    ChatStateRepository,
    MessageRepository,
    WaitingForMeActiveRepository,
)
from echo_v2.persistence.contacts import ContactRepository
from echo_v2.persistence.digest_repositories import DailyDigestRepository
from echo_v2.ports.bot import BotChannel
from echo_v2.runtime.errors import IndeterminateError, PermanentError
from echo_v2.services.digest_formatter import DigestFormatter, DigestItem

__all__ = ["DigestWorker"]

_logger = logging.getLogger("echo_v2.services.digest_worker")

DEFAULT_TZ = "Asia/Jerusalem"
DIGEST_START_HOUR = 8
DIGEST_END_HOUR = 11


class DigestWorker:
    """Polls users and sends morning digests via the Echo Business Bot.

    Args:
        digest_repo: The :class:`DailyDigestRepository` for claim/status.
        active_repo: The :class:`WaitingForMeActiveRepository` for active states.
        chat_state_repo: The :class:`ChatStateRepository` for current versions.
        message_repo: The :class:`MessageRepository` for latest inbound text.
        contact_repo: The :class:`ContactRepository` for name resolution.
        bot: The :class:`BotChannel` to send the digest through.
        user_provider: Callable that returns a list of (user_id, phone, timezone).
        poll_interval_seconds: How often to poll. Default 300 (5 min).
    """

    def __init__(
        self,
        *,
        digest_repo: DailyDigestRepository,
        active_repo: WaitingForMeActiveRepository,
        chat_state_repo: ChatStateRepository,
        message_repo: MessageRepository,
        contact_repo: ContactRepository,
        bot: BotChannel,
        user_provider,
        poll_interval_seconds: float = 300.0,
    ) -> None:
        self._digest_repo = digest_repo
        self._active_repo = active_repo
        self._chat_state_repo = chat_state_repo
        self._message_repo = message_repo
        self._contact_repo = contact_repo
        self._bot = bot
        self._user_provider = user_provider
        self._poll_interval = poll_interval_seconds
        self._formatter = DigestFormatter()

    async def run_once(self, *, now_utc: datetime | None = None) -> int:
        """Process all eligible users once. Returns count of digests sent.

        Args:
            now_utc: The current UTC time. If ``None``, uses
                ``datetime.now(timezone.utc)``. Useful for testing.
        """
        now = now_utc or datetime.now(timezone.utc)
        users = await self._user_provider()
        sent_count = 0
        for user_id, phone, tz_name, first_name in users:
            try:
                if await self._process_user(user_id, phone, tz_name, first_name, now):
                    sent_count += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "error processing digest for user %s, continuing", user_id
                )
        return sent_count

    async def run_loop(self) -> None:
        """Poll for eligible users until cancelled."""
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                _logger.info("digest worker loop cancelled")
                raise
            except Exception:
                _logger.exception("digest worker loop error, continuing")
            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                _logger.info("digest worker loop cancelled during sleep")
                raise

    async def _process_user(
        self,
        user_id: str,
        phone: str,
        tz_name: str | None,
        first_name: str | None,
        now_utc: datetime,
    ) -> bool:
        """Process a single user. Returns ``True`` if a digest was sent."""
        tz = ZoneInfo(tz_name or DEFAULT_TZ)
        local_now = now_utc.astimezone(tz)
        local_date = local_now.date()

        # Check if within digest window.
        if not _in_digest_window(local_now):
            return False

        # Try to claim a daily_digest row.
        digest = await self._digest_repo.claim_or_get(
            user_id=user_id,
            local_date=local_date,
        )
        if digest is None:
            # Already processed today.
            return False

        # Query active waiting chats with version match.
        active_states = await self._get_current_active(user_id)

        if not active_states:
            # No waiting chats — mark as empty so we don't retry later today.
            await self._digest_repo.update_status(
                digest_id=digest.id,
                status=DailyDigestStatus.EMPTY,
                item_count=0,
            )
            _logger.info("digest for user %s on %s: empty", user_id, local_date)
            return False

        # Build digest items.
        items = await self._build_items(user_id, active_states)

        # Format template parameters.
        name = first_name or "חבר"  # fallback if user has no first_name
        params = self._formatter.format(items, first_name=name)

        # Send via bot template.
        try:
            msg_id = await self._bot.send_template(
                phone,
                "morning_waiting_digest",
                "he",
                [params.first_name, params.count, params.items_text],
            )
        except IndeterminateError as exc:
            _logger.warning(
                "digest send indeterminate for user %s: %s", user_id, exc
            )
            await self._digest_repo.update_status(
                digest_id=digest.id,
                status=DailyDigestStatus.INDETERMINATE,
                item_count=len(active_states),
            )
            return False
        except PermanentError as exc:
            _logger.warning(
                "digest send failed for user %s: %s", user_id, exc
            )
            await self._digest_repo.update_status(
                digest_id=digest.id,
                status=DailyDigestStatus.FAILED,
                item_count=len(active_states),
            )
            return False
        except Exception:
            _logger.exception(
                "digest send unexpected error for user %s", user_id
            )
            await self._digest_repo.update_status(
                digest_id=digest.id,
                status=DailyDigestStatus.FAILED,
                item_count=len(active_states),
            )
            return False

        # Success.
        await self._digest_repo.update_status(
            digest_id=digest.id,
            status=DailyDigestStatus.SENT,
            sent_at=datetime.now(timezone.utc),
            provider_message_id=msg_id,
            item_count=len(active_states),
        )
        _logger.info(
            "digest sent to user %s on %s: %d items",
            user_id,
            local_date,
            len(active_states),
        )
        return True

    async def _get_current_active(self, user_id: str) -> list[WaitingForMeActive]:
        """Get active states where target_version matches chats.activity_version."""
        all_active = await self._active_repo.list_all_for_user(user_id=user_id)
        current: list[WaitingForMeActive] = []
        for active in all_active:
            chat = await self._chat_state_repo.get(user_id, active.chat_id)
            if chat is not None and chat.activity_version == active.target_version:
                current.append(active)
        return current

    async def _build_items(
        self,
        user_id: str,
        active_states: list[WaitingForMeActive],
    ) -> list[DigestItem]:
        """Build DigestItems with contact names and last message text.

        Name resolution priority:
        1. chats.chat_name (from Green API senderData.chatName)
        2. contacts.display_name (from contacts table)
        3. message.chat_name or sender_name (from the latest message)
        4. phone number from chat_id (fallback)
        """
        items: list[DigestItem] = []
        for active in active_states:
            phone = _phone_from_chat_id(active.chat_id)

            # Try chat_name from chat state first.
            chat = await self._chat_state_repo.get(user_id, active.chat_id)
            chat_name = chat.chat_name if chat else None

            # Fall back to contact lookup.
            if not chat_name:
                contact = await self._contact_repo.find_by_phone(user_id, phone)
                chat_name = contact.display_name if contact else None

            # Get latest inbound message text (also a name fallback source).
            msg = await self._message_repo.get_latest_inbound(
                user_id=user_id,
                chat_id=active.chat_id,
            )
            last_text = msg.text if msg and msg.text else None

            # Fall back to message-level names.
            if not chat_name and msg:
                chat_name = msg.chat_name or msg.sender_name

            items.append(DigestItem(
                active=active,
                contact_name=chat_name,
                last_message_text=last_text,
            ))
        return items


def _in_digest_window(local_now: datetime) -> bool:
    """Check if local time is within the digest window (08:00–11:00)."""
    hour = local_now.hour
    return DIGEST_START_HOUR <= hour < DIGEST_END_HOUR


def _phone_from_chat_id(chat_id: str) -> str:
    """Extract phone number from a WhatsApp chat ID."""
    return chat_id.split("@")[0] if "@" in chat_id else chat_id
