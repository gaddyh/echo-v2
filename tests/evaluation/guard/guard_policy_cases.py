"""Deterministic policy cases for Guard notification behavior.

These cases are intentionally separate from the LLM analyzer golden cases.
They describe policy inputs directly and can be tested without an API call.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from echo_v2.services.guard_alert_policy import ChildContext, ConversationContext
from echo_v2.services.guard_analyzer import GuardAnalysis

__all__ = ["GUARD_POLICY_CASES", "GuardPolicyCase"]


@dataclass(frozen=True)
class GuardPolicyCase:
    case_id: str
    analysis: GuardAnalysis
    child_context: ChildContext = field(default_factory=ChildContext)
    conversation_context: ConversationContext = field(default_factory=ConversationContext)
    expected_should_alert: bool = False


GUARD_POLICY_CASES: tuple[GuardPolicyCase, ...] = (
    GuardPolicyCase(
        case_id="watch_never_alerts",
        analysis=GuardAnalysis(decision="watch", signals=("secrecy",)),
    ),
    GuardPolicyCase(
        case_id="developing_bullying_does_not_alert",
        analysis=GuardAnalysis(
            decision="concerning",
            categories=("bullying",),
            signals=("repeated_harassment",),
        ),
    ),
    GuardPolicyCase(
        case_id="established_bullying_alerts",
        analysis=GuardAnalysis(
            decision="concerning",
            categories=("bullying",),
            signals=("repeated_harassment", "exclusion"),
        ),
        expected_should_alert=True,
    ),
    GuardPolicyCase(
        case_id="actionable_suspicious_contact_alerts",
        analysis=GuardAnalysis(
            decision="concerning",
            categories=("suspicious_contact",),
            signals=("location_request",),
        ),
        expected_should_alert=True,
    ),
    GuardPolicyCase(
        case_id="urgent_alerts",
        analysis=GuardAnalysis(decision="urgent"),
        expected_should_alert=True,
    ),
    GuardPolicyCase(
        case_id="prior_concerning_alert_is_suppressed",
        analysis=GuardAnalysis(
            decision="concerning",
            categories=("suspicious_contact",),
            signals=("meeting_request",),
        ),
        conversation_context=ConversationContext(prior_alert_sent=True),
    ),
)
