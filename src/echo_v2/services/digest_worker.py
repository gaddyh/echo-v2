"""DigestWorker — polls users and sends morning digests.

Runs as a background task (like ChatAnalysisWorker). On each poll:

1. For each active user:
   a. Compute the user's local time from their timezone.
   b. Check if local time is within the digest window (default 08:00–11:00).
   c. If yes, try to claim a daily_digest row for (user_id, local_date).
   d. If claimed (new row): count actionable waiting items, send template.
   e. If not claimed (row exists): skip — already processed today.

The template only carries the count and a link — no conversation
content is exposed in the template. The count comes from
:meth:`WaitingListQueryService.count_actionable`, which applies the
same actionable filter as the mini-app but doesn't resolve names or
previews.

Send errors:
  - IndeterminateError → status = indeterminate (no blind retry)
  - PermanentError → status = failed
  - Other exceptions → status = failed
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from echo_v2.domain.digest import DailyDigestStatus
from echo_v2.persistence.digest_repositories import DailyDigestRepository
from echo_v2.ports.bot import BotChannel
from echo_v2.runtime.errors import IndeterminateError, PermanentError
from echo_v2.services.waiting_list_query import WaitingListQueryService
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

__all__ = ["DigestWorker"]

_logger = logging.getLogger("echo_v2.services.digest_worker")

DEFAULT_TZ = "Asia/Jerusalem"
DIGEST_START_HOUR = 8
DIGEST_END_HOUR = 11

# Fallback first name when the user has none.
_FALLBACK_NAME = "חבר"


class DigestWorker:
    """Polls users and sends morning digests via the Echo Business Bot.

    Args:
        digest_repo: The :class:`DailyDigestRepository` for claim/status.
        query_service: The shared :class:`WaitingListQueryService`. Only
            :meth:`count_actionable` is used — the digest doesn't need
            names, previews, or summaries.
        bot: The :class:`BotChannel` to send the digest through.
        user_provider: Callable that returns a list of
            (user_id, phone, timezone, first_name).
        token_service: Optional :class:`WaitingListTokenService` for
            issuing waiting-list web sessions. When provided, the
            digest template is sent with a ``url_suffix`` containing
            the session token.
        poll_interval_seconds: How often to poll. Default 300 (5 min).
        template_name: The WhatsApp template name to send.
    """

    def __init__(
        self,
        *,
        digest_repo: DailyDigestRepository,
        query_service: WaitingListQueryService,
        bot: BotChannel,
        user_provider: Callable[[], Awaitable[list[tuple[str, str, str, str | None]]]],
        token_service: WaitingListTokenService | None = None,
        poll_interval_seconds: float = 300.0,
        template_name: str = "morning_waiting_digest6",
    ) -> None:
        self._digest_repo = digest_repo
        self._query_service = query_service
        self._bot = bot
        self._user_provider = user_provider
        self._token_service = token_service
        self._poll_interval = poll_interval_seconds
        self._template_name = template_name

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

        # Count actionable waiting items via the shared query service.
        count = await self._query_service.count_actionable(user_id)

        if count == 0:
            # No waiting chats — mark as empty so we don't retry later today.
            await self._digest_repo.update_status(
                digest_id=digest.id,
                status=DailyDigestStatus.EMPTY,
                item_count=0,
            )
            _logger.info("digest for user %s on %s: empty", user_id, local_date)
            return False

        # Issue a waiting-list web session token (if token service is wired).
        url_suffix: str | None = None
        if self._token_service is not None:
            try:
                _session_id, raw_token = await self._token_service.issue(user_id)
                url_suffix = raw_token
            except Exception:
                _logger.exception(
                    "failed to issue waiting-list token for user %s, "
                    "sending digest without URL button", user_id
                )

        # Send via bot template (2 params: first_name, count).
        name = first_name or _FALLBACK_NAME
        try:
            msg_id = await self._bot.send_template(
                phone,
                self._template_name,
                "he",
                [name, str(count)],
                url_suffix=url_suffix,
            )
        except IndeterminateError as exc:
            _logger.warning(
                "digest send indeterminate for user %s: %s", user_id, exc
            )
            await self._digest_repo.update_status(
                digest_id=digest.id,
                status=DailyDigestStatus.INDETERMINATE,
                item_count=count,
            )
            return False
        except PermanentError as exc:
            _logger.warning(
                "digest send failed for user %s: %s", user_id, exc
            )
            await self._digest_repo.update_status(
                digest_id=digest.id,
                status=DailyDigestStatus.FAILED,
                item_count=count,
            )
            return False
        except Exception:
            _logger.exception(
                "digest send unexpected error for user %s", user_id
            )
            await self._digest_repo.update_status(
                digest_id=digest.id,
                status=DailyDigestStatus.FAILED,
                item_count=count,
            )
            return False

        # Success.
        await self._digest_repo.update_status(
            digest_id=digest.id,
            status=DailyDigestStatus.SENT,
            sent_at=datetime.now(timezone.utc),
            provider_message_id=msg_id,
            item_count=count,
        )
        _logger.info(
            "digest sent to user %s on %s: %d items",
            user_id,
            local_date,
            count,
        )
        return True

    async def send_digest_for_user(
        self,
        user_id: str,
        phone: str,
        tz_name: str | None,
        first_name: str | None,
        *,
        now_utc: datetime | None = None,
    ) -> bool:
        """Send an on-demand digest to a user immediately.

        Skips the digest window check and the daily claim — just counts
        active items, builds the digest, and sends the template.

        Returns ``True`` if sent, ``False`` if no active items or send failed.
        """
        count = await self._query_service.count_actionable(user_id)

        if count == 0:
            return False

        # Issue a waiting-list web session token (if token service is wired).
        url_suffix: str | None = None
        if self._token_service is not None:
            try:
                _session_id, raw_token = await self._token_service.issue(user_id)
                url_suffix = raw_token
            except Exception:
                _logger.exception(
                    "failed to issue waiting-list token for user %s, "
                    "sending digest without URL button", user_id
                )

        # Send via bot template.
        name = first_name or _FALLBACK_NAME
        try:
            await self._bot.send_template(
                phone,
                self._template_name,
                "he",
                [name, str(count)],
                url_suffix=url_suffix,
            )
        except Exception:
            _logger.exception(
                "on-demand digest send failed for user %s", user_id
            )
            return False

        _logger.info(
            "on-demand digest sent to user %s: %d items",
            user_id,
            count,
        )
        return True


def _in_digest_window(local_now: datetime) -> bool:
    """Check if local time is within the digest window (08:00–11:00)."""
    hour = local_now.hour
    return DIGEST_START_HOUR <= hour < DIGEST_END_HOUR
