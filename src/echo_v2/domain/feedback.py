"""Domain types for feedback and actions on waiting-for-me items.

Clean separation:

* **Feedback** — "Was Echo correct?" A learning signal about a specific
  analysis result. Does NOT mutate active state.
* **Action** — "What should Echo do?" An operation on the current active
  item that mutates state (acknowledge, snooze, resolve, mute).

Analysis result = what the model believed.
Feedback        = whether that belief was correct.
Action          = what the user asked Echo to do.
Active state    = what Echo should currently display.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

__all__ = [
    "ChatMute",
    "FeedbackVerdict",
    "WaitingForMeAction",
    "WaitingForMeActionType",
    "WaitingForMeFeedback",
]


class FeedbackVerdict(str, Enum):
    """Was the model's analysis correct?"""

    CORRECT = "correct"
    """Echo was right — the chat was waiting for the user."""

    FALSE_POSITIVE = "false_positive"
    """Echo was wrong — the chat was NOT waiting."""

    FALSE_NEGATIVE = "false_negative"
    """Echo missed a waiting chat (reported via פספסתי)."""

    UNCERTAIN = "uncertain"
    """User is not sure whether Echo was correct."""


class WaitingForMeActionType(str, Enum):
    """What the user asked Echo to do with an active item."""

    ACKNOWLEDGE = "acknowledge"
    """Set acknowledged_at — user is handling it now."""

    SNOOZE = "snooze"
    """Set snoozed_until — remind later."""

    RESOLVE = "resolve"
    """Remove from the active list."""

    MUTE_CHAT = "mute_chat"
    """Mute the chat — suppress from digests and analysis."""

    UNMUTE_CHAT = "unmute_chat"
    """Unmute the chat — resume normal behavior."""


@dataclass(frozen=True)
class WaitingForMeFeedback:
    """A correctness signal on a specific analysis result.

    Attributes:
        user_id: The user who gave the feedback.
        chat_id: The chat the feedback is about.
        result_id: The analysis result being judged (may be None for
            false_negative reports where no result exists).
        target_version: The activity_version the result was computed from.
        verdict: Whether the model was correct.
        conversation_snapshot: JSON of the conversation for training.
        provider_event_id: The WhatsApp callback message ID for dedup.
        created_at: When the feedback was recorded.
        expires_at: When the conversation snapshot should be deleted.
    """

    user_id: str
    chat_id: str
    result_id: str | None
    target_version: int | None
    verdict: FeedbackVerdict
    conversation_snapshot: dict | None = None
    provider_event_id: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class WaitingForMeAction:
    """A user action on an active waiting item.

    Attributes:
        user_id: The user who performed the action.
        chat_id: The chat the action targets.
        active_id: The active item ID (for version validation).
        target_version: The activity_version the active item was at.
        action_type: What the user asked to do.
        action_payload: Additional data (e.g. snoozed_until timestamp).
        provider_event_id: The WhatsApp callback message ID for dedup.
        created_at: When the action was recorded.
    """

    user_id: str
    chat_id: str
    active_id: str | None
    target_version: int | None
    action_type: WaitingForMeActionType
    action_payload: dict | None = None
    provider_event_id: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class ChatMute:
    """A per-user, per-chat mute record.

    Attributes:
        user_id: The user who muted the chat.
        chat_id: The muted chat.
        muted_until: When the mute expires (None if permanent).
        permanent: Whether the mute is permanent.
        created_at: When the mute was created.
        updated_at: When the mute was last updated.
    """

    user_id: str
    chat_id: str
    muted_until: datetime | None
    permanent: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
