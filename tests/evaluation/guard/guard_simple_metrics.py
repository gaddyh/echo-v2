"""Alert-level metric aggregation for Baseline B gold evaluations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvalObservation:
    case_id: str
    checkpoint_id: str
    expected: tuple[bool | None, ...]
    predicted: bool | None


@dataclass(frozen=True)
class AlertMetrics:
    total_checkpoints: int
    strict_alert_checkpoints: int
    flexible_transition_checkpoints: int
    strict_positive_checkpoints: int
    strict_negative_checkpoints: int
    strict_nonnull_outcomes: int
    strict_uncertain_outcomes: int
    flexible_nonnull_outcomes: int
    flexible_uncertain_outcomes: int
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    uncertain_total: int
    uncertain_strict_positive: int
    uncertain_strict_negative: int
    uncertain_flexible: int
    semantic_mismatches: int
    strict_negative_non_alerts: int
    alert_precision: float | None
    alert_recall: float | None
    strict_negative_non_alert_rate: float | None


def summarize_alert_metrics(observations: list[EvalObservation]) -> AlertMetrics:
    """Summarize alert metrics with strict and flexible denominators separated.

    Flexible checkpoints allow both True and a non-alert outcome. They are
    semantically evaluated, but excluded from alert precision/recall because
    they are intentional transition points rather than certain positives or
    negatives. Null outcomes count as uncertain first and are therefore not
    counted as confident true negatives.
    """
    total = len(observations)
    flexible = 0
    strict_positive = 0
    strict_negative = 0
    strict_nonnull = 0
    strict_uncertain = 0
    flexible_nonnull = 0
    flexible_uncertain = 0
    tp = fp = fn = tn = 0
    uncertain_positive = uncertain_negative = uncertain_flexible = 0
    semantic_mismatches = 0

    for item in observations:
        is_flexible = True in item.expected and len(item.expected) > 1
        is_strict_positive = item.expected == (True,)
        is_strict_negative = not is_flexible and True not in item.expected
        if item.predicted not in item.expected:
            semantic_mismatches += 1

        if is_flexible:
            flexible += 1
            if item.predicted is None:
                flexible_uncertain += 1
                uncertain_flexible += 1
            else:
                flexible_nonnull += 1
            continue

        if is_strict_positive:
            strict_positive += 1
        if is_strict_negative:
            strict_negative += 1

        if item.predicted is None:
            strict_uncertain += 1
            if is_strict_positive:
                uncertain_positive += 1
            elif is_strict_negative:
                uncertain_negative += 1
        else:
            strict_nonnull += 1

        if is_strict_positive and item.predicted is True:
            tp += 1
        elif is_strict_positive:
            fn += 1
        elif is_strict_negative and item.predicted is True:
            fp += 1
        elif is_strict_negative and item.predicted is False:
            tn += 1

    uncertain_total = strict_uncertain + flexible_uncertain
    strict_negative_non_alerts = tn + uncertain_negative
    precision_denominator = tp + fp
    return AlertMetrics(
        total_checkpoints=total,
        strict_alert_checkpoints=total - flexible,
        flexible_transition_checkpoints=flexible,
        strict_positive_checkpoints=strict_positive,
        strict_negative_checkpoints=strict_negative,
        strict_nonnull_outcomes=strict_nonnull,
        strict_uncertain_outcomes=strict_uncertain,
        flexible_nonnull_outcomes=flexible_nonnull,
        flexible_uncertain_outcomes=flexible_uncertain,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
        uncertain_total=uncertain_total,
        uncertain_strict_positive=uncertain_positive,
        uncertain_strict_negative=uncertain_negative,
        uncertain_flexible=uncertain_flexible,
        semantic_mismatches=semantic_mismatches,
        strict_negative_non_alerts=strict_negative_non_alerts,
        alert_precision=tp / precision_denominator if precision_denominator else None,
        alert_recall=tp / strict_positive if strict_positive else None,
        strict_negative_non_alert_rate=(
            strict_negative_non_alerts / strict_negative if strict_negative else None
        ),
    )


def format_alert_metrics(metrics: AlertMetrics) -> str:
    precision = f"{metrics.alert_precision:.3f}" if metrics.alert_precision is not None else "n/a"
    recall = f"{metrics.alert_recall:.3f}" if metrics.alert_recall is not None else "n/a"
    non_alert_rate = (
        f"{metrics.strict_negative_non_alert_rate:.3f}"
        if metrics.strict_negative_non_alert_rate is not None
        else "n/a"
    )
    return "\n".join(
        [
            "Baseline B evaluation metrics",
            f"  semantic denominator:            {metrics.total_checkpoints}/{metrics.total_checkpoints} checkpoints",
            f"  semantic mismatches:             {metrics.semantic_mismatches}",
            f"  strict alert denominator:        {metrics.strict_alert_checkpoints} checkpoints",
            f"  flexible transition denominator: {metrics.flexible_transition_checkpoints} checkpoints",
            f"  strict outcomes accounted:       {metrics.strict_nonnull_outcomes} non-null + {metrics.strict_uncertain_outcomes} uncertain = {metrics.strict_alert_checkpoints}",
            f"  flexible outcomes accounted:     {metrics.flexible_nonnull_outcomes} non-null + {metrics.flexible_uncertain_outcomes} uncertain = {metrics.flexible_transition_checkpoints}",
            f"  true positives:                  {metrics.true_positives}",
            f"  false positives:                 {metrics.false_positives}",
            f"  false negatives:                 {metrics.false_negatives}",
            f"  true negatives:                  {metrics.true_negatives}",
            f"  uncertain results:               {metrics.uncertain_total} total = {metrics.uncertain_strict_positive} strict-positive + {metrics.uncertain_strict_negative} strict-negative + {metrics.uncertain_flexible} flexible",
            f"  strict-negative non-alerts:      {metrics.strict_negative_non_alerts}/{metrics.strict_negative_checkpoints} ({non_alert_rate})",
            f"  alert precision:                 {precision} over {metrics.true_positives + metrics.false_positives} emitted alerts",
            f"  alert recall:                    {recall} over {metrics.strict_positive_checkpoints} strict-positive checkpoints",
        ]
    )
