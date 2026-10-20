"""Deterministic alert policy for Guard analyses.

The Guard analyzer detects what is happening and how serious it is. This
module owns the separate product decision of whether a parent should be
notified now. Keeping that decision deterministic means alert behavior can
evolve with product policy without prompt tuning or detector retraining.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Protocol

from echo_v2.services.guard_analyzer import GuardAnalysis
from echo_v2.services.guard_decision_policy import DefaultDecisionPolicy
from echo_v2.services.guard_signal_ledger import GuardSignalState
from echo_v2.services.guard_taxonomy import (
    GuardCategory,
    GuardDecision,
    GuardSignal,
)

GUARD_ALERT_POLICY_VERSION = "2026-10-20.multi-guardian"

__all__ = [
    "AlertKind",
    "AlertOutcome",
    "AlertPolicy",
    "ChildContext",
    "ConversationContext",
    "DefaultAlertPolicy",
]


class AlertKind(str, Enum):
    NONE = "none"
    NOTIFY = "notify"
    URGENT = "urgent"


class _AlertRank(IntEnum):
    NONE = 0
    NOTIFY = 1
    URGENT = 2


_ALERT_RANKS = {
    AlertKind.NONE: _AlertRank.NONE,
    AlertKind.NOTIFY: _AlertRank.NOTIFY,
    AlertKind.URGENT: _AlertRank.URGENT,
}


@dataclass(frozen=True)
class AlertOutcome:
    should_alert: bool
    event: bool
    kind: AlertKind
    reason: str = ""
    threshold_family: str | None = None


@dataclass(frozen=True)
class ChildContext:
    """Non-conversation facts that may affect notification policy."""

    age: int | None = None
    known_contact: bool | None = None


@dataclass(frozen=True)
class ConversationContext:
    """Conversation and notification history known to the policy."""

    prior_alert_kind: AlertKind = AlertKind.NONE
    prior_alert_sent: bool = False
    quiet_hours: bool = False

    def effective_prior_alert_kind(self) -> AlertKind:
        if self.prior_alert_kind is not AlertKind.NONE:
            return self.prior_alert_kind
        return AlertKind.NOTIFY if self.prior_alert_sent else AlertKind.NONE


class AlertPolicy(Protocol):
    """Protocol for deciding whether to notify a parent."""

    def evaluate(
        self,
        *,
        analysis: GuardAnalysis,
        child_context: ChildContext,
        conversation_context: ConversationContext,
        signal_state: GuardSignalState | None = None,
    ) -> AlertOutcome:
        raise NotImplementedError

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
    """Initial deterministic Guard notification policy."""

    def evaluate(
        self,
        *,
        analysis: GuardAnalysis,
        child_context: ChildContext,
        conversation_context: ConversationContext,
        signal_state: GuardSignalState | None = None,
    ) -> AlertOutcome:
        del child_context
        decision = DefaultDecisionPolicy().decide(
            signals=analysis.signals,
            categories=analysis.categories,
            signal_state=signal_state,
        )
        threshold_family = self._threshold_family(
            analysis=analysis,
            signal_state=signal_state,
        )
        if decision is GuardDecision.URGENT:
            kind = AlertKind.URGENT
            reason = "Immediate or escalating high-risk evidence."
        elif self._actionable(
            analysis=analysis,
            signal_state=signal_state,
            conversation_context=conversation_context,
        ):
            kind = AlertKind.NOTIFY
            reason = "A deterministic Guard alert threshold was reached."
        else:
            kind = AlertKind.NONE
            reason = ""
        event = (
            kind is not AlertKind.NONE
            and _ALERT_RANKS[kind]
            > _ALERT_RANKS[conversation_context.effective_prior_alert_kind()]
            and not conversation_context.quiet_hours
        )
        return AlertOutcome(
            should_alert=kind is not AlertKind.NONE,
            event=event,
            kind=kind,
            reason=reason,
            threshold_family=threshold_family if kind is not AlertKind.NONE else None,
        )

    @staticmethod
    def _threshold_family(
        *, analysis: GuardAnalysis, signal_state: GuardSignalState | None
    ) -> str | None:
        signals = {
            GuardSignal(signal)
            for signal in (signal_state.active_signals if signal_state else analysis.signals)
        }
        categories = {
            GuardCategory(category)
            for category in (
                signal_state.active_categories if signal_state else analysis.categories
            )
        }
        if GuardSignal.SELF_HARM_EXPRESSION in signals:
            return "urgent_self_harm"
        if GuardSignal.BLACKMAIL_OR_EXTORTION in signals:
            return "urgent_blackmail"
        if GuardCategory.SUSPICIOUS_CONTACT in categories and signals & {
            GuardSignal.LOCATION_REQUEST,
            GuardSignal.MEETING_REQUEST,
        }:
            return "suspicious_contact_location_or_meeting"
        if GuardCategory.BULLYING in categories and {
            GuardSignal.REPEATED_TARGETING,
            GuardSignal.GROUP_PILE_ON,
        } <= signals:
            return "bullying_established"
        if GuardCategory.HARASSMENT_OR_COERCION in categories and GuardSignal.THREAT in signals:
            return "harassment_explicit_threat"
        if GuardCategory.CHILD_SEXUAL_EXPLOITATION in categories:
            return "child_sexual_exploitation"
        if GuardCategory.HARMFUL_SHARING in categories and GuardSignal.THREAT_TO_SHARE in signals:
            return "harmful_sharing_threat"
        return None

    def should_alert(
        self,
        *,
        analysis: GuardAnalysis,
        child_context: ChildContext,
        conversation_context: ConversationContext,
        signal_state: GuardSignalState | None = None,
    ) -> bool:
        return self.evaluate(
            analysis=analysis,
            child_context=child_context,
            conversation_context=conversation_context,
            signal_state=signal_state,
        ).event

    @staticmethod
    def _actionable(
        *,
        analysis: GuardAnalysis,
        signal_state: GuardSignalState | None,
        conversation_context: ConversationContext,
    ) -> bool:
        if conversation_context.effective_prior_alert_kind() is not AlertKind.NONE or conversation_context.quiet_hours:
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
            and bool(signals & {GuardSignal.LOCATION_REQUEST, GuardSignal.MEETING_REQUEST})
        )
        bullying_is_established = (
            GuardCategory.BULLYING in categories
            and {GuardSignal.REPEATED_TARGETING, GuardSignal.GROUP_PILE_ON} <= signals
        )
        explicit_threat = (
            GuardCategory.HARASSMENT_OR_COERCION in categories
            and GuardSignal.THREAT in signals
        )
        return suspicious_contact_is_actionable or bullying_is_established or explicit_threat
