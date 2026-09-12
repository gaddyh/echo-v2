"""WaitingForMeDecision — the output contract of chat analysis.

The single question the analysis answers:

    Is there an open request, question, or commitment in this conversation
    for which the next step is expected from the user?

Three answers:

* :data:`WaitingForMeDecision.WAITING_FOR_ME` — there is an open request,
  question, or expectation directed at the user. The ball is in the user's
  court.

* :data:`WaitingForMeDecision.NOT_WAITING_FOR_ME` — no open expectation.
  The last message is informational, a closing, or an acknowledgment.

* :data:`WaitingForMeDecision.UNCERTAIN` — not enough information to decide
  confidently. In POC, UNCERTAIN is not shown to the user but is stored for
  evaluation.

This module defines only the contract — the enum and a lightweight result
dataclass. The LLM prompt and parsing logic come in a later stage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

__all__ = [
    "WaitingForMeActive",
    "WaitingForMeDecision",
    "WaitingForMeResult",
]


class WaitingForMeDecision(str, Enum):
    """The three possible answers to 'is the next step expected from the user?'."""

    WAITING_FOR_ME = "waiting_for_me"
    """An open request, question, or expectation is directed at the user.

    Examples:
    - Direct question: "Are you free on Thursday?"
    - Request for action: "Can you send me the file?"
    - Request for confirmation: "Do you approve the price?"
    - Request for decision: "What did you decide?"
    - Follow-up on commitment: "Did you update the client already?"
    - Explicit waiting: "Waiting for your answer"
    """

    NOT_WAITING_FOR_ME = "not_waiting_for_me"
    """No open expectation. The ball is not in the user's court.

    Examples:
    - Closing: "Thanks", "Got it", "Closed", "Great"
    - Status update: "I'll update you", "Sent it to you for reference"
    - Informational: "The meeting is at 3pm"
    """

    UNCERTAIN = "uncertain"
    """Not enough information to decide confidently.

    Examples:
    - Media-only message (image/voice) without accessible content
    - Very ambiguous phrasing
    - Missing earlier context
    - Unclear who is being addressed (group)
    - Contradictory expectations in the conversation

    In POC: UNCERTAIN is not shown to the user but is stored for evaluation.
    """


@dataclass(frozen=True)
class WaitingForMeResult:
    """The full output of a single chat analysis.

    Attributes:
        decision: The :class:`WaitingForMeDecision` verdict.
        confidence: Optional 0.0–1.0 confidence score from the LLM.
        reason: Optional short explanation of why this decision was reached.
            Internal/technical — not shown to the user.
        summary: Optional one-sentence user-facing summary in Hebrew.
            Describes the situation and what is being waited for, without
            the contact name (shown separately). Max ~160 chars.
        target_version: The ``activity_version`` that was analyzed. Used
            by the worker's version check to discard stale results.
        conversation_snapshot: JSON snapshot of the conversation as it
            was at analysis time. Stored so feedback can reference the
            exact messages the model saw, not messages loaded later.
    """

    decision: WaitingForMeDecision
    confidence: float | None = None
    reason: str | None = None
    summary: str | None = None
    target_version: int = 0
    conversation_snapshot: dict | None = None


@dataclass(frozen=True)
class WaitingForMeActive:
    """Current active waiting state for a chat — one row per chat.

    Attributes:
        id: Surrogate UUID identifying this active row. Used in callback IDs
            so we don't expose provider-specific chat_id in button payloads.
        user_id: The user who owns this chat.
        chat_id: The WhatsApp chat ID.
        target_version: The ``activity_version`` this state was computed
            from. Stale if ``target_version != chats.activity_version``.
        result_id: The ``waiting_for_me_results.id`` that produced this
            active state.
        waiting_since: When the waiting state originally started.
            Preserved across re-analyses that remain WAITING_FOR_ME.
        notified_at: When the user was last notified about this waiting
            state. ``None`` if not yet notified.
        acknowledged_at: When the user tapped "מטפל עכשיו". ``None`` if
            not yet acknowledged. Reset to ``None`` when a new analysis
            version arrives.
        snoozed_until: When the snooze expires. ``None`` if not snoozed.
            While snoozed, the item is suppressed from digests.
    """

    id: str
    user_id: str
    chat_id: str
    target_version: int
    result_id: str
    waiting_since: datetime
    notified_at: datetime | None = None
    acknowledged_at: datetime | None = None
    snoozed_until: datetime | None = None
