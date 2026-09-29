"""Tests for the deterministic Guard alert policy."""

from __future__ import annotations

import pytest

from echo_v2.services.guard_alert_policy import (
    ChildContext,
    ConversationContext,
    DefaultAlertPolicy,
)
from echo_v2.services.guard_analyzer import GuardAnalysis


@pytest.fixture
def policy() -> DefaultAlertPolicy:
    return DefaultAlertPolicy()


def _analysis(
    decision: str,
    *,
    signals: tuple[str, ...] = (),
    categories: tuple[str, ...] = (),
) -> GuardAnalysis:
    return GuardAnalysis(
        decision=decision,  # type: ignore[arg-type]
        signals=signals,
        categories=categories,
    )


def _should_alert(
    policy: DefaultAlertPolicy,
    analysis: GuardAnalysis,
    *,
    conversation_context: ConversationContext | None = None,
) -> bool:
    return policy.should_alert(
        analysis=analysis,
        child_context=ChildContext(),
        conversation_context=conversation_context or ConversationContext(),
    )


def test_none_and_watch_never_alert(policy: DefaultAlertPolicy) -> None:
    assert not _should_alert(policy, _analysis("none"))
    assert not _should_alert(policy, _analysis("watch", signals=("secrecy",)))


def test_urgent_always_alerts(policy: DefaultAlertPolicy) -> None:
    assert _should_alert(policy, _analysis("urgent"))


def test_developing_bullying_does_not_alert(policy: DefaultAlertPolicy) -> None:
    analysis = _analysis(
        "concerning",
        signals=("repeated_harassment",),
        categories=("bullying",),
    )

    assert not _should_alert(policy, analysis)


def test_established_bullying_alerts(policy: DefaultAlertPolicy) -> None:
    analysis = _analysis(
        "concerning",
        signals=("repeated_harassment", "exclusion"),
        categories=("bullying",),
    )

    assert _should_alert(policy, analysis)


def test_actionable_suspicious_contact_alerts(policy: DefaultAlertPolicy) -> None:
    analysis = _analysis(
        "concerning",
        signals=("location_request",),
        categories=("suspicious_contact",),
    )

    assert _should_alert(policy, analysis)


def test_prior_alert_suppresses_duplicate_concerning_alert(
    policy: DefaultAlertPolicy,
) -> None:
    analysis = _analysis(
        "concerning",
        signals=("location_request",),
        categories=("suspicious_contact",),
    )

    assert not _should_alert(
        policy,
        analysis,
        conversation_context=ConversationContext(prior_alert_sent=True),
    )
