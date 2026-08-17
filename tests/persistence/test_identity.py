"""Unit tests for phone-number normalization (no Docker needed)."""

from __future__ import annotations

import pytest

from echo_v2.persistence.identity import PhoneParseError, normalize_phone_e164


def test_local_il_number_normalizes_to_e164():
    assert normalize_phone_e164("0546610653") == "+972546610653"


def test_international_without_plus_normalizes_to_e164():
    assert normalize_phone_e164("972546610653") == "+972546610653"


def test_already_e164_normalizes_to_e164():
    assert normalize_phone_e164("+972546610653") == "+972546610653"


def test_three_forms_collapse_to_one_identity():
    a = normalize_phone_e164("0546610653")
    b = normalize_phone_e164("972546610653")
    c = normalize_phone_e164("+972546610653")
    assert a == b == c == "+972546610653"


def test_us_number_with_explicit_country_code():
    assert normalize_phone_e164("+12125550100") == "+12125550100"


def test_us_number_with_default_region_us():
    assert normalize_phone_e164("2125550100", default_region="US") == "+12125550100"


def test_invalid_number_raises_phone_parse_error():
    with pytest.raises(PhoneParseError):
        normalize_phone_e164("not-a-number")


def test_empty_string_raises_phone_parse_error():
    with pytest.raises(PhoneParseError):
        normalize_phone_e164("")


def test_too_short_raises_phone_parse_error():
    with pytest.raises(PhoneParseError):
        normalize_phone_e164("123")


def test_garbage_digits_raises_phone_parse_error():
    with pytest.raises(PhoneParseError):
        normalize_phone_e164("0000000000")


def test_normalization_strips_whitespace():
    assert normalize_phone_e164(" 0546610653 ") == "+972546610653"


def test_normalization_strips_dashes():
    assert normalize_phone_e164("054-661-0653") == "+972546610653"
