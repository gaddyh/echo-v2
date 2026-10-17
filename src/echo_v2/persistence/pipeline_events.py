"""Persistence port for structured pipeline events."""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from echo_v2.domain.pipeline_event import PipelineEvent

_logger = logging.getLogger("echo_v2.persistence.pipeline_events")


@runtime_checkable
class PipelineEventRepository(Protocol):
    async def record(self, event: PipelineEvent) -> None: ...


class LoggingPipelineEventRepository:
    """Safe fallback sink used when no database event repository is configured."""

    async def record(self, event: PipelineEvent) -> None:
        _logger.info(
            "pipeline event name=%s pipeline=%s status=%s message_id=%s event_id=%s "
            "attempt=%s duration_ms=%s error_type=%s metadata=%s",
            event.event_name,
            event.pipeline,
            event.status,
            event.message_id,
            event.event_id,
            event.attempt,
            event.duration_ms,
            event.error_type,
            event.metadata,
        )


__all__ = ["LoggingPipelineEventRepository", "PipelineEventRepository"]
