"""Deterministic alert policy for Guard analyses.

The Guard analyzer detects what is happening and how serious it is. This
module owns the separate product decision of whether a parent should be
notified now. Keeping that decision deterministic means alert behavior can
evolve with product policy without prompt tuning or detector retraining.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from echo_v2.services.guard_analyzer import GuardAnalysis
from echo_v2.services.guard_signal_ledger import GuardSignalState
from echo_v2.services.guard_taxonomy import (
    GuardCategory,
    GuardDecision,
    GuardSignal,
)

__all__ = [
    "AlertPolicy",
    "ChildContext",
    "ConversationContext",
    "DefaultAlertPolicy",
]


@dataclass(frozen=True)
class ChildContext:
    """Non-conversation facts that may affect notification policy."""

    age: int | None = None
    known_contact: bool | None = None


@dataclass(frozen=True)
class ConversationContext:
    """Conversation and notification history known to the policy."""

    prior_alert_sent: bool = False
    quiet_hours: bool = False


class AlertPolicy(Protocol):
    """Policy interface for deciding whether to notify a parent."""

    def should_alert(
        self,
        *,
        analysis: GuardAnalysis,
        child_context: ChildContext,
        conversation_context: ConversationContext,
        signal_state: GuardSignalState | None = None,
    ) -> bool:
        raise NotImplementedError


class DefaultAlertPolicy:
    """Initial deterministic Guard notification policy.

    ``watch`` never alerts. ``urgent`` always alerts. A ``concerning``
    analysis alerts only when it contains an actionable category/signal
    combination; a developing pattern alone is monitored without notifying.
    """

    def should_alert(
        self,
        *,
        analysis: GuardAnalysis,
        child_context: ChildContext,
        conversation_context: ConversationContext,
        signal_state: GuardSignalState | None = None,
    ) -> bool:
        del child_context

        if analysis.decision in {GuardDecision.NONE, GuardDecision.WATCH}:
            return False
        if analysis.decision == GuardDecision.URGENT:
            return True
        if conversation_context.prior_alert_sent:
            return False
        if conversation_context.quiet_hours:
            return False

        signals = {
            GuardSignal(signal)
            for signal in (signal_state.active_signals if signal_state else analysis.signals)
        }
        categories = {
            GuardCategory(category)
            for category in (
                signal_state.active_categories
                if signal_state
                else analysis.categories
            )
        }

        suspicious_contact_is_actionable = (
            GuardCategory.SUSPICIOUS_CONTACT in categories
            and bool(
                signals
                & {GuardSignal.LOCATION_REQUEST, GuardSignal.MEETING_REQUEST}
            )
        )
        bullying_is_established = (
            GuardCategory.BULLYING in categories
            and {
                GuardSignal.REPEATED_TARGETING,
                GuardSignal.GROUP_PILE_ON,
            }
            <= signals
        )
        explicit_threat = (
            GuardCategory.HARASSMENT_OR_COERCION in categories
            and GuardSignal.THREAT in signals
        )

        return (
            suspicious_contact_is_actionable
            or bullying_is_established
            or explicit_threat
        )
