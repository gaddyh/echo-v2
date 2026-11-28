"""Consume durable WAHA inbox rows into Echo chat ingestion.

The WAHA analogue of :class:`BaileysMessageConsumer`: claim pending rows →
parse the GOWS envelope into the provider-neutral message contract →
resolve the WAHA session to an Echo user via ``whatsapp_connections``
(provider ``waha``) → :meth:`ChatIngestionService.ingest_message` →
complete the claim (revision-guarded). Analysis, media enrichment and
transcription happen in the standard workers afterwards, exactly like the
Baileys flow.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time

from echo_v2.domain.waha import ClaimedWahaMessage
from echo_v2.persistence.waha_event_inbox import WahaEventInboxRepository
from echo_v2.persistence.whatsapp_connections import WhatsAppConnectionRepository
from echo_v2.services.chat_ingestion import ChatIngestionService
from echo_v2.services.waha_messages import parse_envelope

__all__ = ["WahaMessageConsumer"]

_logger = logging.getLogger("echo_v2.services.waha_message_consumer")

_PROVIDER = "waha"


class WahaMessageConsumer:
    def __init__(
        self,
        repository: WahaEventInboxRepository,
        connections: WhatsAppConnectionRepository,
        ingestion_service: ChatIngestionService,
        *,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 50,
        lease_seconds: int = 60,
        max_attempts: int = 10,
        backlog_alert_seconds: float = 3600.0,
        backlog_check_interval_seconds: float = 60.0,
        worker_id: str | None = None,
    ) -> None:
        self._repository = repository
        self._connections = connections
        self._ingestion = ingestion_service
        self._poll_interval_seconds = poll_interval_seconds
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._backlog_alert_seconds = backlog_alert_seconds
        self._backlog_check_interval_seconds = backlog_check_interval_seconds
        self._worker_id = worker_id or f"echo-waha-message-{socket.gethostname()}"
        self._last_backlog_check = 0.0

    async def _warn_on_stale_backlog(self, now: float) -> None:
        """Media CDN URLs expire (~hours): a growing pending age is urgent.

        Throttled to one check per ``backlog_check_interval_seconds``.
        """
        if now - self._last_backlog_check < self._backlog_check_interval_seconds:
            return
        self._last_backlog_check = now
        try:
            pending, failed, oldest_age = await self._repository.backlog_stats()
        except Exception:
            _logger.warning("waha backlog stats query failed", exc_info=True)
            return
        if oldest_age is not None and oldest_age > self._backlog_alert_seconds:
            _logger.warning(
                "waha inbox backlog aging pending=%d failed=%d oldest_age_seconds=%.0f "
                "threshold_seconds=%.0f — media CDN URLs may expire before processing",
                pending,
                failed,
                oldest_age,
                self._backlog_alert_seconds,
            )

    async def run_once(self) -> int:
        claimed = await self._repository.claim_pending(
            self._worker_id,
            batch_size=self._batch_size,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )
        processed = 0
        if claimed:
            _logger.info("waha message batch claimed count=%d", len(claimed))
        suppressed_items: list[ClaimedWahaMessage] = []
        for item in claimed:
            try:
                event = parse_envelope(item.message)
                if event.is_history_sync:
                    # History-sync replays are suppressed before persistence:
                    # no message row, no analysis scheduling, no media
                    # enrichment. The claims are bulk-completed below so a
                    # multi-thousand-row sync drains in a handful of
                    # statements instead of a round-trip per row.
                    suppressed_items.append(item)
                    continue
                connection = await self._connections.get_by_provider_id(
                    _PROVIDER, item.message.session
                )
                if connection is None or connection.id is None:
                    raise ValueError(
                        "unknown Echo WAHA connection — link the session with "
                        "scripts/link_waha_session.py"
                    )
                await self._ingestion.ingest_message(
                    event,
                    user_id=connection.user_id,
                    connection_id=connection.id,
                )
                if not await self._repository.complete_claimed(item):
                    raise RuntimeError(
                        "WAHA message claim was lost or the revision advanced "
                        "before completion"
                    )
                processed += 1
                _logger.info(
                    "waha message processing complete session=%s message_id=%s",
                    item.message.session,
                    item.message.message_id,
                )
            except Exception as exc:  # noqa: BLE001 - one bad row must not abort the batch
                _logger.warning(
                    "waha message processing failed session=%s message_id=%s "
                    "error_type=%s",
                    item.message.session,
                    item.message.message_id,
                    type(exc).__name__,
                )
                await self._repository.mark_failed(item, str(exc))
        completed_suppressed = 0
        if suppressed_items:
            completed_suppressed = await self._repository.complete_claimed_many(
                suppressed_items
            )
            _logger.info(
                "waha history-sync envelopes suppressed count=%d completed=%d",
                len(suppressed_items),
                completed_suppressed,
            )
        return processed + completed_suppressed

    async def run_loop(self) -> None:
        while True:
            try:
                await self._warn_on_stale_backlog(time.monotonic())
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("WAHA message consumer loop error")
                processed = 0
            if processed == 0:
                await asyncio.sleep(self._poll_interval_seconds)
