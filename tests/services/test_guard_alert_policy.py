"""Tests for the deterministic Guard alert policy."""

from __future__ import annotations

import pytest

from echo_v2.services.guard_alert_policy import DefaultAlertPolicy
from tests.evaluation.guard.guard_policy_cases import GUARD_POLICY_CASES


@pytest.mark.parametrize("case", GUARD_POLICY_CASES, ids=lambda case: case.case_id)
def test_guard_policy(case) -> None:
    actual = DefaultAlertPolicy().should_alert(
        analysis=case.analysis,
        child_context=case.child_context,
        conversation_context=case.conversation_context,
    )

    assert actual is case.expected_should_alert
