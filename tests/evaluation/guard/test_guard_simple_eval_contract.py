"""API-free contract checks for the Baseline B Gold prefixes."""

from __future__ import annotations

from echo_v2.domain.guard_simple import ChildRole
from tests.evaluation.guard.guard_simple_cases import CASES, validate_cases
from tests.evaluation.guard.guard_simple_metrics import (
    EvalObservation,
    format_alert_metrics,
    summarize_alert_metrics,
)


def test_baseline_b_gold_cases_are_valid() -> None:
    validate_cases()


def test_gold_cases_cover_prefixes_and_expected_parent_attention() -> None:
    assert len(CASES) == 18
    assert all(len(case.checkpoints) >= 2 for case in CASES)
    assert all(
        checkpoint.expected_parent_should_know
        for case in CASES
        for checkpoint in case.checkpoints
    )
    assert all(
        value in (True, False, None)
        for case in CASES
        for checkpoint in case.checkpoints
        for value in checkpoint.expected_parent_should_know
    )
    assert any(
        ChildRole.TARGET in checkpoint.expected_child_roles
        for case in CASES
        for checkpoint in case.checkpoints
    )


def test_alert_metric_denominators_are_explicit_and_non_overlapping() -> None:
    observations = [
        *[EvalObservation("positive", str(index), (True,), True) for index in range(10)],
        *[EvalObservation("negative", str(index), (False,), False) for index in range(36)],
        *[EvalObservation("negative-null", str(index), (None, False), None) for index in range(4)],
        *[EvalObservation("flexible", str(index), (None, True), True) for index in range(5)],
        *[EvalObservation("flexible-null", str(index), (None, True), None) for index in range(2)],
    ]

    metrics = summarize_alert_metrics(observations)

    assert metrics.total_checkpoints == 57
    assert metrics.strict_alert_checkpoints == 50
    assert metrics.flexible_transition_checkpoints == 7
    assert metrics.strict_nonnull_outcomes == 46
    assert metrics.strict_uncertain_outcomes == 4
    assert metrics.strict_nonnull_outcomes + metrics.strict_uncertain_outcomes == 50
    assert metrics.flexible_nonnull_outcomes == 5
    assert metrics.flexible_uncertain_outcomes == 2
    assert metrics.flexible_nonnull_outcomes + metrics.flexible_uncertain_outcomes == 7
    assert metrics.uncertain_total == 6
    assert metrics.uncertain_strict_positive == 0
    assert metrics.uncertain_strict_negative == 4
    assert metrics.uncertain_flexible == 2
    assert metrics.true_positives == 10
    assert metrics.false_positives == 0
    assert metrics.false_negatives == 0
    assert metrics.true_negatives == 36
    assert metrics.strict_negative_non_alerts == 40
    assert metrics.strict_negative_checkpoints == 40
    assert metrics.alert_precision == 1
    assert metrics.alert_recall == 1


def test_uncertain_strict_positive_counts_as_false_negative() -> None:
    metrics = summarize_alert_metrics(
        [
            EvalObservation("positive", "certain", (True,), True),
            EvalObservation("positive", "uncertain", (True,), None),
            EvalObservation("negative", "negative", (False,), False),
        ]
    )

    assert metrics.strict_positive_checkpoints == 2
    assert metrics.true_positives == 1
    assert metrics.false_negatives == 1
    assert metrics.uncertain_strict_positive == 1
    assert metrics.alert_recall == 0.5
    assert metrics.strict_nonnull_outcomes + metrics.strict_uncertain_outcomes == metrics.strict_alert_checkpoints


def test_alert_metric_output_names_each_denominator() -> None:
    output = format_alert_metrics(
        summarize_alert_metrics(
            [
                EvalObservation("positive", "positive", (True,), True),
                EvalObservation("negative", "negative", (False,), False),
                EvalObservation("flexible", "flexible", (None, True), None),
            ]
        )
    )

    assert "semantic denominator" in output
    assert "strict alert denominator" in output
    assert "flexible transition denominator" in output
    assert "strict outcomes accounted" in output
    assert "flexible outcomes accounted" in output
    assert "strict-negative non-alerts" in output
    assert "emitted alerts" in output
    assert "strict-positive checkpoints" in output
