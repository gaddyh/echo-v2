"""Deterministic severity policy for Guard analyses."""

from __future__ import annotations

from echo_v2.services.guard_signal_ledger import GuardSignalState
from echo_v2.services.guard_taxonomy import (
    GuardCategory,
    GuardDecision,
    GuardSignal,
)

__all__ = ["DecisionPolicy", "DefaultDecisionPolicy"]


class DecisionPolicy:
    """Protocol for deriving product severity from semantic evidence."""

    def decide(
        self,
        *,
        signals: tuple[str, ...],
        categories: tuple[str, ...],
        signal_state: GuardSignalState | None = None,
    ) -> GuardDecision:
        raise NotImplementedError


class DefaultDecisionPolicy:
    """Initial deterministic severity policy for the MVP taxonomy."""

    def decide(
        self,
        *,
        signals: tuple[str, ...],
        categories: tuple[str, ...],
        signal_state: GuardSignalState | None = None,
    ) -> GuardDecision:
        if signal_state is not None:
            signals = signal_state.active_signals
            categories = signal_state.active_categories

        signal_set = {GuardSignal(signal) for signal in signals}
        category_set = {GuardCategory(category) for category in categories}

        if not signal_set:
            return GuardDecision.NONE

        if (
            GuardSignal.SELF_HARM_EXPRESSION in signal_set
            or GuardSignal.BLACKMAIL_OR_EXTORTION in signal_set
            or {
                GuardSignal.SECRECY_REQUEST,
                GuardSignal.MEETING_REQUEST,
            }
            <= signal_set
        ):
            return GuardDecision.URGENT

        if len(signal_set) >= 2 or len(category_set) >= 1:
            return GuardDecision.CONCERNING

        return GuardDecision.WATCH
