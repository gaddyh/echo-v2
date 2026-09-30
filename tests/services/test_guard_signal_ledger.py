"""Tests for cumulative Guard signal state."""

from echo_v2.services.guard_analyzer import GuardAnalysis
from echo_v2.services.guard_signal_ledger import GuardSignalLedger
from echo_v2.services.guard_taxonomy import GuardDecision


def test_omitted_signal_is_not_removed() -> None:
    ledger = GuardSignalLedger()

    ledger.update(
        GuardAnalysis(
            decision=GuardDecision.CONCERNING,
            categories=("suspicious_contact",),
            signals=("location_request",),
            evidence_message_ids=("m5",),
        )
    )
    state = ledger.update(
        GuardAnalysis(
            decision=GuardDecision.CONCERNING,
            categories=(),
            signals=("secrecy",),
            evidence_message_ids=("m7",),
        )
    )

    assert state.active_signals == ("location_request", "secrecy")
    assert state.evidence_by_signal["location_request"] == ("m5",)
    assert state.evidence_by_signal["secrecy"] == ("m7",)


def test_new_evidence_is_merged_without_duplicate_ids() -> None:
    ledger = GuardSignalLedger()
    analysis = GuardAnalysis(
        decision=GuardDecision.CONCERNING,
        signals=("location_request",),
        evidence_message_ids=("m3", "m5"),
    )

    ledger.update(analysis)
    state = ledger.update(analysis)

    assert state.active_signals == ("location_request",)
    assert state.evidence_by_signal["location_request"] == ("m3", "m5")
