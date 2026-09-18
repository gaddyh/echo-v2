"""Bot command dispatch — typed commands parsed from wire events.

Replaces the chain of pre-handlers in the 360dialog webhook with a
small explicit command dispatcher.

Architecture:

    wire event (BotEvent)
        ↓
    BotCommandParser.parse(event)
        ↓
    typed BotCommand (ResponsibilityDone, OnboardingCode, ...)
        ↓
    BotCommandRouter dispatches to the right service

Wire formats stay as-is (``action:{id}:handled``, ``dismiss:{id}:reason``,
``onboarding:start``). The parser normalizes them into typed internal
commands at the boundary, so business handlers never inspect raw
callback strings.

Text keywords ("קוד", "סיכום חדש", "צפה בשיחות", cancel words) are also
parsed into typed commands. New aliases can be added in the parser
without touching any handler.
"""

from __future__ import annotations

from dataclasses import dataclass

from echo_v2.ports.bot import BotEvent, BotEventType

__all__ = [
    "BotCommand",
    "BotCommandParser",
    "Cancel",
    "DigestOpen",
    "ListDone",
    "OnboardingCode",
    "OnboardingConnect",
    "OnboardingInfo",
    "OnboardingQr",
    "OnboardingShowQr",
    "OnboardingStart",
    "ResponsibilityDismiss",
    "ResponsibilityDone",
    "ResponsibilityList",
    "ResponsibilitySnooze",
]


# --- Typed commands --------------------------------------------------------


@dataclass(frozen=True)
class ResponsibilityDone:
    """User tapped 'טופל' — resolve the active item."""

    responsibility_id: str


@dataclass(frozen=True)
class ResponsibilitySnooze:
    """User tapped 'להזכיר לי' — snooze the active item."""

    responsibility_id: str
    preset: str | None = None  # "1h", "tomorrow", None (default 1h)


@dataclass(frozen=True)
class ResponsibilityDismiss:
    """User tapped 'לא צריד' or a dismiss submenu reason."""

    responsibility_id: str
    reason: str | None = None  # "not_waiting", "not_interested", None (open submenu)


@dataclass(frozen=True)
class ResponsibilityList:
    """User sent 'צפה בשיחות' — send waiting-list cards."""


@dataclass(frozen=True)
class DigestOpen:
    """User sent 'סיכום חדש' — send the waiting-list link."""


@dataclass(frozen=True)
class ListDone:
    """User sent 'סיימתי לעבור על רשימת ההמתנה' — acknowledge review done."""


@dataclass(frozen=True)
class OnboardingStart:
    """Unknown user consented (button tap or text 'חברו אותי')."""


@dataclass(frozen=True)
class OnboardingConnect:
    """Pending user tapped 'חבר אותי' — start pairing (claim/create instance)."""


@dataclass(frozen=True)
class OnboardingShowQr:
    """Pending user tapped 'הצג QR' — fetch + send QR for an existing connection."""


@dataclass(frozen=True)
class OnboardingInfo:
    """User tapped 'איך זה עובד?' — send explanation."""


@dataclass(frozen=True)
class OnboardingCode:
    """Known user sent 'קוד' — resend OTP."""


@dataclass(frozen=True)
class OnboardingQr:
    """Known user sent 'qr' — resend QR image."""


@dataclass(frozen=True)
class Cancel:
    """User sent a cancel keyword — cancel the active flow."""


BotCommand = (
    ResponsibilityDone
    | ResponsibilitySnooze
    | ResponsibilityDismiss
    | ResponsibilityList
    | DigestOpen
    | ListDone
    | OnboardingStart
    | OnboardingConnect
    | OnboardingShowQr
    | OnboardingInfo
    | OnboardingCode
    | OnboardingQr
    | Cancel
)


# --- Text keyword aliases ---------------------------------------------------

_CONSENT_PHRASE = "חברו אותי"
_CODE_KEYWORD = "קוד"
_QR_KEYWORD = "qr"
_DIGEST_KEYWORD = "סיכום חדש"
_VIEW_DETAILS_BUTTON = "צפה בשיחות"
_LIST_DONE_TEXT = "סיימתי לעבור על רשימת ההמתנה"
_CANCEL_KEYWORDS = {"cancel", "בטל", "ביטול", "stop"}


# --- Parser -----------------------------------------------------------------


class BotCommandParser:
    """Parse a :class:`BotEvent` into a typed :class:`BotCommand`.

    Recognizes:
    * Button callbacks: ``action:{id}:handled``, ``action:{id}:snooze``,
      ``action:{id}:snooze:{preset}``, ``action:{id}:dismiss``,
      ``dismiss:{id}:{reason}``, ``onboarding:start``, ``onboarding:info``,
      ``onboarding:connect``, ``onboarding:show_qr``
    * Text keywords: ``קוד``, ``qr``, ``סיכום חדש``, ``צפה בשיחות``,
      ``סיימתי לעבור על רשימת ההמתנה``, ``חברו אותי``, cancel words.

    Returns ``None`` if the event doesn't match any command — the
    router then falls through to stateful flows / fallback.
    """

    def parse(self, event: BotEvent) -> BotCommand | None:
        if event.type is BotEventType.BUTTON_REPLY and event.button_id:
            return self._parse_callback(event.button_id)
        if event.type is BotEventType.TEXT and event.text:
            return self._parse_text(event.text)
        return None

    @staticmethod
    def _parse_callback(callback_id: str) -> BotCommand | None:
        # action:{active_id}:{action_type}[:preset]
        if callback_id.startswith("action:"):
            parts = callback_id.split(":", 2)
            if len(parts) < 3:
                return None
            active_id = parts[1]
            action_type = parts[2]
            if action_type == "handled":
                return ResponsibilityDone(responsibility_id=active_id)
            if action_type == "snooze":
                return ResponsibilitySnooze(responsibility_id=active_id)
            if action_type.startswith("snooze:"):
                preset = action_type.split(":", 1)[1]
                return ResponsibilitySnooze(
                    responsibility_id=active_id, preset=preset,
                )
            if action_type == "dismiss":
                return ResponsibilityDismiss(responsibility_id=active_id)
            return None

        # dismiss:{active_id}:{reason}
        if callback_id.startswith("dismiss:"):
            parts = callback_id.split(":", 2)
            if len(parts) < 3:
                return None
            active_id = parts[1]
            reason = parts[2]
            return ResponsibilityDismiss(
                responsibility_id=active_id, reason=reason,
            )

        # onboarding:start, onboarding:info, onboarding:connect, onboarding:show_qr
        if callback_id == "onboarding:start":
            return OnboardingStart()
        if callback_id == "onboarding:connect":
            return OnboardingConnect()
        if callback_id == "onboarding:show_qr":
            return OnboardingShowQr()
        if callback_id == "onboarding:info":
            return OnboardingInfo()

        return None

    @staticmethod
    def _parse_text(text: str) -> BotCommand | None:
        stripped = text.strip()

        if stripped == _CODE_KEYWORD:
            return OnboardingCode()
        if stripped.lower() == _QR_KEYWORD:
            return OnboardingQr()
        if _DIGEST_KEYWORD in stripped:
            return DigestOpen()
        if stripped.strip("[]") == _VIEW_DETAILS_BUTTON:
            return ResponsibilityList()
        if _LIST_DONE_TEXT in stripped:
            return ListDone()
        if stripped == _CONSENT_PHRASE:
            return OnboardingStart()
        if stripped.lower() in _CANCEL_KEYWORDS:
            return Cancel()

        return None
