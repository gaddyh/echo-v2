"""Tests for the deterministic Guard alert policy."""

from __future__ import annotations

import pytest

from echo_v2.services.guard_alert_policy import DefaultAlertPolicy
from echo_v2.services.guard_analyzer import GuardAnalysis
from echo_v2.services.guard_decision_policy import DefaultDecisionPolicy
from echo_v2.services.guard_signal_ledger import GuardSignalLedger
from echo_v2.services.guard_taxonomy import (
    GuardCategory,
    GuardSignal,
)
from tests.evaluation.guard.guard_policy_cases import GUARD_POLICY_CASES


@pytest.mark.parametrize("case", GUARD_POLICY_CASES, ids=lambda case: case.case_id)
def test_guard_policy(case) -> None:
    actual = DefaultAlertPolicy().should_alert(
        analysis=case.analysis,
        child_context=case.child_context,
        conversation_context=case.conversation_context,
    )

    assert (
        DefaultDecisionPolicy().decide(
            signals=case.analysis.signals,
            categories=case.analysis.categories,
        )
        is case.expected_decision
    )
    assert actual is case.expected_should_alert


def test_policy_uses_accumulated_signals_when_latest_analysis_omits_one() -> None:
    ledger = GuardSignalLedger()
    ledger.update(
        GuardAnalysis(
            categories=(GuardCategory.SUSPICIOUS_CONTACT,),
            signals=(GuardSignal.LOCATION_REQUEST,),
            evidence_message_ids=("m5",),
        )
    )
    state = ledger.update(
        GuardAnalysis(
            categories=(),
            signals=(GuardSignal.SECRECY_REQUEST,),
            evidence_message_ids=("m7",),
        )
    )

    should_alert = DefaultAlertPolicy().should_alert(
        analysis=GuardAnalysis(),
        child_context=GUARD_POLICY_CASES[0].child_context,
        conversation_context=GUARD_POLICY_CASES[0].conversation_context,
        signal_state=state,
    )

    assert should_alert is True
