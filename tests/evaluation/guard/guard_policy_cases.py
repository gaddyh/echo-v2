"""Deterministic policy cases for Guard notification behavior.

These cases are intentionally separate from the LLM analyzer golden cases.
They describe policy inputs directly and can be tested without an API call.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from echo_v2.services.guard_alert_policy import ChildContext, ConversationContext
from echo_v2.services.guard_analyzer import GuardAnalysis
from echo_v2.services.guard_taxonomy import (
    GuardCategory,
    GuardDecision,
    GuardSignal,
)

__all__ = ["GUARD_POLICY_CASES", "GuardPolicyCase"]


@dataclass(frozen=True)
class GuardPolicyCase:
    case_id: str
    analysis: GuardAnalysis
    child_context: ChildContext = field(default_factory=ChildContext)
    conversation_context: ConversationContext = field(default_factory=ConversationContext)
    expected_decision: GuardDecision = GuardDecision.WATCH
    expected_should_alert: bool = False


GUARD_POLICY_CASES: tuple[GuardPolicyCase, ...] = (
    GuardPolicyCase(
        case_id="watch_never_alerts",
        analysis=GuardAnalysis(signals=(GuardSignal.SECRECY_REQUEST,)),
        expected_decision=GuardDecision.WATCH,
    ),
    GuardPolicyCase(
        case_id="developing_bullying_does_not_alert",
        analysis=GuardAnalysis(
            categories=(GuardCategory.BULLYING,),
            signals=(GuardSignal.REPEATED_TARGETING,),
        ),
        expected_decision=GuardDecision.CONCERNING,
    ),
    GuardPolicyCase(
        case_id="established_bullying_alerts",
        analysis=GuardAnalysis(
            categories=(GuardCategory.BULLYING,),
            signals=(GuardSignal.REPEATED_TARGETING, GuardSignal.GROUP_PILE_ON),
        ),
        expected_decision=GuardDecision.CONCERNING,
        expected_should_alert=True,
    ),
    GuardPolicyCase(
        case_id="actionable_suspicious_contact_alerts",
        analysis=GuardAnalysis(
            categories=(GuardCategory.SUSPICIOUS_CONTACT,),
            signals=(GuardSignal.LOCATION_REQUEST,),
        ),
        expected_decision=GuardDecision.CONCERNING,
        expected_should_alert=True,
    ),
    GuardPolicyCase(
        case_id="urgent_alerts",
        analysis=GuardAnalysis(signals=(GuardSignal.BLACKMAIL_OR_EXTORTION,)),
        expected_decision=GuardDecision.URGENT,
        expected_should_alert=True,
    ),
    GuardPolicyCase(
        case_id="prior_concerning_alert_is_suppressed",
        analysis=GuardAnalysis(
            categories=(GuardCategory.SUSPICIOUS_CONTACT,),
            signals=(GuardSignal.MEETING_REQUEST,),
        ),
        conversation_context=ConversationContext(prior_alert_sent=True),
        expected_decision=GuardDecision.CONCERNING,
    ),
)
