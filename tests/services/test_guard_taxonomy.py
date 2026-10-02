"""Tests for the code-owned Guard MVP taxonomy."""

from __future__ import annotations

import pytest

from echo_v2.services.guard_taxonomy import (
    CATEGORY_SIGNALS,
    GUARD_TAXONOMY_VERSION,
    SIGNAL_CATEGORIES,
    GuardCategory,
    GuardSignal,
    validate_category_signal_relationships,
)


def test_mvp_taxonomy_version_and_categories() -> None:
    assert GUARD_TAXONOMY_VERSION == "v1"
    assert {category.value for category in GuardCategory} == {
        "suspicious_contact",
        "child_sexual_exploitation",
        "harassment_or_coercion",
        "distress",
        "bullying",
        "social_exclusion",
        "harmful_sharing",
    }


def test_every_signal_has_derived_reverse_lookup() -> None:
    assert set(SIGNAL_CATEGORIES) == set(GuardSignal)
    assert all(SIGNAL_CATEGORIES[signal] for signal in GuardSignal)


def test_category_signal_relationships_are_many_to_many_metadata() -> None:
    assert GuardSignal.THREAT in CATEGORY_SIGNALS[GuardCategory.HARASSMENT_OR_COERCION]
    assert (
        GuardSignal.INTIMATE_IMAGE_REQUEST
        in CATEGORY_SIGNALS[GuardCategory.CHILD_SEXUAL_EXPLOITATION]
    )
    assert (
        GuardSignal.THREAT_TO_SHARE
        in CATEGORY_SIGNALS[GuardCategory.CHILD_SEXUAL_EXPLOITATION]
    )
    assert GuardSignal.REPEATED_TARGETING in CATEGORY_SIGNALS[GuardCategory.BULLYING]
    assert GuardSignal.EXCLUSION in CATEGORY_SIGNALS[GuardCategory.SOCIAL_EXCLUSION]
    assert GuardCategory.HARASSMENT_OR_COERCION in SIGNAL_CATEGORIES[GuardSignal.THREAT]


def test_category_requires_supporting_signal() -> None:
    validate_category_signal_relationships(
        [GuardCategory.SUSPICIOUS_CONTACT],
        [GuardSignal.LOCATION_REQUEST],
    )

    with pytest.raises(ValueError, match="no supporting signal"):
        validate_category_signal_relationships(
            [GuardCategory.SUSPICIOUS_CONTACT],
            [GuardSignal.SELF_HARM_EXPRESSION],
        )
