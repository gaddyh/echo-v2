"""Tests for the WaitingForMeDecision contract."""

from __future__ import annotations

from echo_v2.domain.waiting_for_me import WaitingForMeDecision, WaitingForMeResult


def test_decision_enum_values():
    assert WaitingForMeDecision.WAITING_FOR_ME.value == "waiting_for_me"
    assert WaitingForMeDecision.NOT_WAITING_FOR_ME.value == "not_waiting_for_me"
    assert WaitingForMeDecision.UNCERTAIN.value == "uncertain"


def test_decision_enum_count():
    assert len(WaitingForMeDecision) == 3


def test_decision_is_str_enum():
    """WaitingForMeDecision is a str Enum so it serializes naturally."""
    assert isinstance(WaitingForMeDecision.WAITING_FOR_ME, str)
    assert WaitingForMeDecision.WAITING_FOR_ME == "waiting_for_me"


def test_result_minimal():
    r = WaitingForMeResult(decision=WaitingForMeDecision.WAITING_FOR_ME)
    assert r.decision == WaitingForMeDecision.WAITING_FOR_ME
    assert r.confidence is None
    assert r.reason is None
    assert r.target_version == 0


def test_result_full():
    r = WaitingForMeResult(
        decision=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        confidence=0.92,
        reason="The last message is a closing acknowledgment.",
        target_version=3,
    )
    assert r.decision == WaitingForMeDecision.NOT_WAITING_FOR_ME
    assert r.confidence == 0.92
    assert r.reason == "The last message is a closing acknowledgment."
    assert r.target_version == 3


def test_result_is_frozen():
    r = WaitingForMeResult(decision=WaitingForMeDecision.UNCERTAIN)
    try:
        r.confidence = 0.5  # type: ignore[misc]
        raise AssertionError("Should have raised FrozenInstanceError")
    except AttributeError:
        pass  # dataclass(frozen=True) raises AttributeError on assignment


def test_result_with_each_decision():
    for decision in WaitingForMeDecision:
        r = WaitingForMeResult(decision=decision, target_version=1)
        assert r.decision is decision
