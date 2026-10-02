"""Code-owned MVP taxonomy for Guard categories and signals."""

from __future__ import annotations

from enum import Enum

__all__ = [
    "CATEGORY_SIGNALS",
    "GUARD_TAXONOMY_VERSION",
    "SIGNAL_CATEGORIES",
    "GuardCategory",
    "GuardDecision",
    "GuardSignal",
    "validate_category_signal_relationships",
]

GUARD_TAXONOMY_VERSION = "v1"


class GuardDecision(str, Enum):
    NONE = "none"
    WATCH = "watch"
    CONCERNING = "concerning"
    URGENT = "urgent"


class GuardCategory(str, Enum):
    SUSPICIOUS_CONTACT = "suspicious_contact"
    CHILD_SEXUAL_EXPLOITATION = "child_sexual_exploitation"
    HARASSMENT_OR_COERCION = "harassment_or_coercion"
    DISTRESS = "distress"
    BULLYING = "bullying"
    SOCIAL_EXCLUSION = "social_exclusion"
    HARMFUL_SHARING = "harmful_sharing"


class GuardSignal(str, Enum):
    OFFLINE_KNOWLEDGE = "offline_knowledge"
    PERSONAL_INFORMATION_REQUEST = "personal_information_request"
    ROUTINE_PROBING = "routine_probing"
    LOCATION_REQUEST = "location_request"
    SECRECY_REQUEST = "secrecy_request"
    MEETING_REQUEST = "meeting_request"
    AGE_DECEPTION = "age_deception"
    SEXUAL_SOLICITATION = "sexual_solicitation"
    INTIMATE_IMAGE_REQUEST = "intimate_image_request"
    SEXUAL_COERCION = "sexual_coercion"
    OFF_PLATFORM_MIGRATION = "off_platform_migration"
    REPEATED_UNWANTED_CONTACT = "repeated_unwanted_contact"
    BOUNDARY_VIOLATION = "boundary_violation"
    THREAT = "threat"
    COERCIVE_DEMAND = "coercive_demand"
    BLACKMAIL_OR_EXTORTION = "blackmail_or_extortion"
    HELP_REQUEST = "help_request"
    FEAR_EXPRESSION = "fear_expression"
    HOPELESSNESS = "hopelessness"
    SELF_HARM_EXPRESSION = "self_harm_expression"
    REPEATED_TARGETING = "repeated_targeting"
    INSULT_OR_HUMILIATION = "insult_or_humiliation"
    GROUP_PILE_ON = "group_pile_on"
    EXCLUSION = "exclusion"
    COORDINATED_EXCLUSION = "coordinated_exclusion"
    HARMFUL_CONTENT_SHARING = "harmful_content_sharing"
    THREAT_TO_SHARE = "threat_to_share"


CATEGORY_SIGNALS: dict[GuardCategory, frozenset[GuardSignal]] = {
    GuardCategory.SUSPICIOUS_CONTACT: frozenset(
        {
            GuardSignal.OFFLINE_KNOWLEDGE,
            GuardSignal.PERSONAL_INFORMATION_REQUEST,
            GuardSignal.ROUTINE_PROBING,
            GuardSignal.LOCATION_REQUEST,
            GuardSignal.SECRECY_REQUEST,
            GuardSignal.MEETING_REQUEST,
        }
    ),
    GuardCategory.CHILD_SEXUAL_EXPLOITATION: frozenset(
        {
            GuardSignal.AGE_DECEPTION,
            GuardSignal.SEXUAL_SOLICITATION,
            GuardSignal.INTIMATE_IMAGE_REQUEST,
            GuardSignal.SEXUAL_COERCION,
            GuardSignal.OFF_PLATFORM_MIGRATION,
            GuardSignal.SECRECY_REQUEST,
            GuardSignal.LOCATION_REQUEST,
            GuardSignal.MEETING_REQUEST,
            GuardSignal.THREAT_TO_SHARE,
            GuardSignal.BLACKMAIL_OR_EXTORTION,
        }
    ),
    GuardCategory.HARASSMENT_OR_COERCION: frozenset(
        {
            GuardSignal.REPEATED_UNWANTED_CONTACT,
            GuardSignal.BOUNDARY_VIOLATION,
            GuardSignal.THREAT,
            GuardSignal.COERCIVE_DEMAND,
            GuardSignal.BLACKMAIL_OR_EXTORTION,
        }
    ),
    GuardCategory.DISTRESS: frozenset(
        {
            GuardSignal.HELP_REQUEST,
            GuardSignal.FEAR_EXPRESSION,
            GuardSignal.HOPELESSNESS,
            GuardSignal.SELF_HARM_EXPRESSION,
        }
    ),
    GuardCategory.BULLYING: frozenset(
        {
            GuardSignal.REPEATED_TARGETING,
            GuardSignal.INSULT_OR_HUMILIATION,
            GuardSignal.GROUP_PILE_ON,
        }
    ),
    GuardCategory.SOCIAL_EXCLUSION: frozenset(
        {GuardSignal.EXCLUSION, GuardSignal.COORDINATED_EXCLUSION}
    ),
    GuardCategory.HARMFUL_SHARING: frozenset(
        {GuardSignal.HARMFUL_CONTENT_SHARING, GuardSignal.THREAT_TO_SHARE}
    ),
}

SIGNAL_CATEGORIES: dict[GuardSignal, frozenset[GuardCategory]] = {
    signal: frozenset(
        category for category, signals in CATEGORY_SIGNALS.items() if signal in signals
    )
    for signal in GuardSignal
}


def validate_category_signal_relationships(
    categories: list[GuardCategory] | tuple[str, ...],
    signals: list[GuardSignal] | tuple[str, ...],
) -> None:
    """Require every returned category to have at least one supporting signal."""
    signal_values = {
        signal.value if isinstance(signal, GuardSignal) else signal
        for signal in signals
    }
    for category in categories:
        category_value = category.value if isinstance(category, GuardCategory) else category
        supported = {
            signal.value
            for signal in CATEGORY_SIGNALS[GuardCategory(category_value)]
        }
        if not signal_values & supported:
            raise ValueError(
                f"Category '{category_value}' has no supporting signal"
            )
