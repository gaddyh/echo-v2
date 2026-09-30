"""Deterministic cumulative signal state for Guard analyses.

The LLM output is an immutable observation of one conversation prefix. This
module derives monotonic signal state across prefixes so a later omission by
the model does not erase previously established evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from echo_v2.services.guard_analyzer import GuardAnalysis

__all__ = ["GuardSignalLedger", "GuardSignalState"]


@dataclass(frozen=True)
class GuardSignalState:
    """Signals and categories established across analyzed prefixes."""

    active_signals: tuple[str, ...] = ()
    active_categories: tuple[str, ...] = ()
    evidence_by_signal: dict[str, tuple[str, ...]] = field(default_factory=dict)


class GuardSignalLedger:
    """Accumulate Guard signals without rewriting raw analyzer observations."""

    def __init__(self) -> None:
        self._signals: list[str] = []
        self._categories: list[str] = []
        self._evidence_by_signal: dict[str, list[str]] = {}

    def update(self, analysis: GuardAnalysis) -> GuardSignalState:
        """Merge one analysis into the derived cumulative signal state.

        Absence from a later analysis is not treated as a retraction. Explicit
        signal retraction is intentionally not supported until the product has
        a defined contradiction/resolution contract.
        """
        for signal in analysis.signals:
            if signal not in self._signals:
                self._signals.append(signal)
            evidence = self._evidence_by_signal.setdefault(signal, [])
            for message_id in analysis.evidence_message_ids:
                if message_id not in evidence:
                    evidence.append(message_id)

        for category in analysis.categories:
            if category not in self._categories:
                self._categories.append(category)

        return self.state()

    def state(self) -> GuardSignalState:
        """Return the current immutable-ish derived state snapshot."""
        return GuardSignalState(
            active_signals=tuple(self._signals),
            active_categories=tuple(self._categories),
            evidence_by_signal={
                signal: tuple(evidence)
                for signal, evidence in self._evidence_by_signal.items()
            },
        )
