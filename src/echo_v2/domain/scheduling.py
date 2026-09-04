"""ScheduledAction domain model.

A generic persistent future action (roadmap §8.4). The scheduler persists
actions to the database, claims due ones via the repository, and executes
them through :func:`echo_v2.runtime.executor.execute` with idempotency —
so a restart or retry never causes the same WhatsApp message to be sent
twice.

Initial action type is ``SEND_WHATSAPP_MESSAGE``. ``SEND_REMINDER`` is
declared for later use (Echo Business Bot notifications); its execution
path is not implemented yet.

Status transitions::

    PENDING --claim_due--> IN_PROGRESS --success--> SUCCEEDED
                              IN_PROGRESS --permanent failure--> FAILED
                              IN_PROGRESS --indeterminate--> INDETERMINATE
                              IN_PROGRESS --stale (recover)--> PENDING
    PENDING --cancel--> CANCELLED

``INDETERMINATE`` means the send may or may not have happened (timeout/5xx
on an irreversible write). The action stays in this state and is not
automatically retried — the idempotency store caches the outcome, so a
blind retry would replay the indeterminate result. Human reconciliation
or a reconciliation mechanism is required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

__all__ = [
    "ScheduledAction",
    "ScheduledActionStatus",
    "ScheduledActionType",
]


class ScheduledActionType(Enum):
    """What kind of future action this is."""

    SEND_WHATSAPP_MESSAGE = "send_whatsapp_message"
    SEND_REMINDER = "send_reminder"
    SEND_BOT_MESSAGE = "send_bot_message"


class ScheduledActionStatus(Enum):
    """Lifecycle state of a scheduled action."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"


@dataclass
class ScheduledAction:
    """A persisted future action.

    ``payload`` is a provider-neutral dict whose shape depends on ``type``:
    * ``SEND_WHATSAPP_MESSAGE`` → ``{"chat_id": "...", "message": "..."}``
    * ``SEND_REMINDER`` → ``{"message": "..."}`` (delivered via Echo Bot)

    ``execute_at_utc`` is always stored in UTC; ``timezone`` retains the
    user's source timezone for display. ``result`` is set on success and
    carries ``{"provider_message_id": "..."}`` for delivery tracking.
    ``claimed_at`` is set when the scheduler claims the action (status →
    ``IN_PROGRESS``); it drives stale-lease recovery on restart.
    """

    id: str
    user_id: str
    type: ScheduledActionType
    execute_at_utc: datetime
    timezone: str
    status: ScheduledActionStatus
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    executed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    claimed_at: datetime | None = None
