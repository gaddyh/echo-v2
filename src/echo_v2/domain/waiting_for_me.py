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
from enum import Enum

__all__ = [
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
        target_version: The ``activity_version`` that was analyzed. Used
            by the worker's version check to discard stale results.
    """

    decision: WaitingForMeDecision
    confidence: float | None = None
    reason: str | None = None
    target_version: int = 0
