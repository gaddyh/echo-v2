"""Bot command dispatch package.

Typed commands parsed from wire events, replacing the chain of
pre-handlers in the 360dialog webhook.
"""

from echo_v2.bot.commands import (
    BotCommand,
    BotCommandParser,
    Cancel,
    DigestOpen,
    ListDone,
    OnboardingCode,
    OnboardingInfo,
    OnboardingStart,
    ResponsibilityDismiss,
    ResponsibilityDone,
    ResponsibilityList,
    ResponsibilitySnooze,
)
from echo_v2.bot.router import BotCommandRouter

__all__ = [
    "BotCommand",
    "BotCommandParser",
    "BotCommandRouter",
    "Cancel",
    "DigestOpen",
    "ListDone",
    "OnboardingCode",
    "OnboardingInfo",
    "OnboardingStart",
    "ResponsibilityDismiss",
    "ResponsibilityDone",
    "ResponsibilityList",
    "ResponsibilitySnooze",
]
