"""Consume durable Baileys connector connection-state events."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

from langsmith import traceable

from echo_v2.observability.sanitizers import (
    safe_baileys_connection_inputs,
    safe_baileys_connection_output,
)
from echo_v2.observability.tracing import tracing_client
from echo_v2.persistence.baileys_events import BaileysEventRepository
from echo_v2.ports.whatsapp import ConnectionStatus

__all__ = ["BaileysConnectionStateConsumer"]

_logger = logging.getLogger("echo_v2.services.baileys_connection_state_consumer")

_DISCONNECTED_MESSAGE = (
    "החיבור של Echo ל־WhatsApp התנתק.\n"
    "שלח קוד כדי להתחבר מחדש."
)


async def _trace_connection_state(event: Any, *, processed: bool) -> dict[str, Any]:
    status = getattr(getattr(event, "status", None), "value", "unknown")
    runtime_state = getattr(event, "runtime_state", None) or "unknown"

    @traceable(
        name=f"guard.baileys.state.{status}.{runtime_state}",
        client=tracing_client,
        process_inputs=safe_baileys_connection_inputs,
        process_outputs=safe_baileys_connection_output,
    )
    async def emit(event: Any, *, processed: bool) -> dict[str, Any]:
        return {
            "status": getattr(getattr(event, "status", None), "value", None),
            "provider_raw_status": getattr(event, "provider_raw_status", None),
            "runtime_state": getattr(event, "runtime_state", None),
            "processed": processed,
        }

    return await emit(event, processed=processed)


class BaileysConnectionStateConsumer:
    def __init__(
        self,
        repository: BaileysEventRepository,
        *,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 50,
        lease_seconds: int = 60,
        max_attempts: int = 10,
        worker_id: str | None = None,
        onboarding_service: Any | None = None,
    ) -> None:
        self._repository = repository
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._onboarding = onboarding_service
        self._worker_id = worker_id or f"echo-baileys-{socket.gethostname()}"

    async def run_once(self) -> int:
        claimed = await self._repository.claim_batch(
            self._worker_id,
            batch_size=self._batch_size,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )
        processed = 0
        for item in claimed:
            try:
                if item.event.status is ConnectionStatus.CONNECTED and self._onboarding is not None:
                    child_id = await self._repository.reconnect_child_for_event(
                        item.event.connection_id, item.event.state_version
                    )
                    if child_id is not None:
                        await self._onboarding.handle_connection_established_by_id(child_id)
                await self._repository.process_claimed_event(
                    item,
                    notification_message=_DISCONNECTED_MESSAGE,
                )
                await _trace_connection_state(item.event, processed=True)
                processed += 1
            except Exception as exc:
                await _trace_connection_state(item.event, processed=False)
                _logger.exception(
                    "Baileys state event processing failed event_id=%s",
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
            except asyncio.CancelledError:  # pragma: no cover - cancellation propagates at await
                raise
            except Exception:
                _logger.exception("Baileys state consumer loop error")
                processed = 0
            if processed == 0:
                await asyncio.sleep(self._poll_interval)
