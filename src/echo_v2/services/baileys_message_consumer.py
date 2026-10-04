"""Consume durable Baileys message events into Echo chat ingestion."""

from __future__ import annotations

import asyncio
import logging
import socket

from echo_v2.integrations.baileys.events import BaileysMessageEvent
from echo_v2.persistence.baileys_events import (
    BaileysEventRepository,
    BaileysMessageEnvelope,
)
from echo_v2.services.chat_ingestion import ChatIngestionService

__all__ = ["BaileysMessageConsumer"]

_logger = logging.getLogger("echo_v2.services.baileys_message_consumer")


class BaileysMessageConsumer:
    def __init__(
        self,
        repository: BaileysEventRepository,
        ingestion_service: ChatIngestionService,
        *,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 50,
        lease_seconds: int = 60,
        max_attempts: int = 10,
        worker_id: str | None = None,
    ) -> None:
        self._repository = repository
        self._ingestion = ingestion_service
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._worker_id = worker_id or f"echo-baileys-message-{socket.gethostname()}"

    async def run_once(self) -> int:
        claimed = await self._repository.claim_batch(
            self._worker_id,
            batch_size=self._batch_size,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
            event_type="message",
        )
        processed = 0
        for item in claimed:
            try:
                if not isinstance(item.event, BaileysMessageEnvelope):
                    raise TypeError("message consumer received a connection-state event")
                event = BaileysMessageEvent.from_payload(item.event.payload)
                resolved = await self._repository.resolve_message_user(event)
                if resolved is None:
                    raise ValueError("unknown Echo Baileys connection")
                user_id, connection_id = resolved
                await self._ingestion.ingest_message(
                    event,
                    user_id=user_id,
                    connection_id=connection_id,
                )
                if not await self._repository.complete_claimed_event(item):
                    raise RuntimeError("Baileys message claim was lost before completion")
                processed += 1
            except Exception as exc:
                _logger.exception(
                    "Baileys message event processing failed event_id=%s",
                    item.event.event_id,
                )
                await self._repository.mark_failed(
                    item.event.event_id,
                    item.worker_id,
                    str(exc),
                )
        return processed

    async def run_loop(self) -> None:
        while True:
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Baileys message consumer loop error")
                processed = 0
            if processed == 0:
                await asyncio.sleep(self._poll_interval)
