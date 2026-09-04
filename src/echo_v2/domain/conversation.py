"""Scheduling flow conversation state.

Per-user ephemeral state for the multi-turn scheduling flow:

```
IDLE → vCard received → AWAITING_MESSAGE (store recipient)
AWAITING_MESSAGE → text received → AWAITING_TIME (store message)
AWAITING_TIME → text received → SCHEDULED (parse time, create action)
```

This is **not** the full ConversationService (roadmap §9.1, Step 2).
It is a lightweight state machine for the scheduling flow only. State is
ephemeral — if the process restarts, the user simply starts over. This
is acceptable because no external side effect has happened yet (the
scheduled action is only created at the end).

The state is keyed by ``user_id`` (resolved from the bot sender's phone
number by the application layer).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

__all__ = [
    "SchedulingFlowContext",
    "SchedulingFlowState",
]


class SchedulingFlowState(Enum):
    """State of the scheduling flow for a user."""

    IDLE = "idle"
    AWAITING_MESSAGE = "awaiting_message"
    AWAITING_TIME = "awaiting_time"


@dataclass
class SchedulingFlowContext:
    """Per-user scheduling flow state.

    ``recipient_phone`` and ``recipient_name`` are set when the user sends
    a vCard. ``message`` is set when the user sends the text to schedule.
    ``updated_at`` tracks the last interaction for expiry.
    """

    user_id: str
    state: SchedulingFlowState = SchedulingFlowState.IDLE
    recipient_phone: str | None = None
    recipient_name: str | None = None
    message: str | None = None
    is_reminder: bool = False
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
