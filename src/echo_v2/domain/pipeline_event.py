"""Structured operational events emitted by analysis pipelines."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class PipelineEvent:
    event_name: str
    pipeline: str
    status: str
    message_id: str | None = None
    event_id: str | None = None
    user_id_hash: str | None = None
    chat_id_hash: str | None = None
    attempt: int | None = None
    duration_ms: float | None = None
    error_type: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


__all__ = ["PipelineEvent"]
