"""Tests for the Baseline B Security and Exposure WhatsApp templates."""

from __future__ import annotations

from scripts.create_guard_parent_attention_template import (
    EXPOSURE_TEMPLATE_NAME,
    SECURITY_TEMPLATE_NAME,
    template_payload,
    template_payloads,
)


def _body(payload: dict[str, object]) -> dict[str, object]:
    components = payload["components"]
    assert isinstance(components, list)
    body = next(component for component in components if component["type"] == "BODY")
    assert isinstance(body, dict)
    return body


def test_security_template_is_recognizable_and_has_no_button() -> None:
    payload = template_payload(SECURITY_TEMPLATE_NAME)
    assert payload["name"] == SECURITY_TEMPLATE_NAME
    components = payload["components"]
    assert isinstance(components, list)
    header = next(component for component in components if component["type"] == "HEADER")
    assert header["text"] == "התראת בטיחות אישית"
    body = _body(payload)
    assert body["text"] == (
        "🚨 זוהה חשש לפגיעה ב{{1}}.\n\n{{2}}\n\nמומלץ לשוחח עם הילד/ה בהקדם."
    )
    assert not any(component["type"] == "BUTTONS" for component in components)


def test_exposure_template_is_calm_and_neutral() -> None:
    payload = template_payload(EXPOSURE_TEMPLATE_NAME)
    assert payload["name"] == EXPOSURE_TEMPLATE_NAME
    components = payload["components"]
    assert isinstance(components, list)
    header = next(component for component in components if component["type"] == "HEADER")
    assert header["text"] == "עדכון על סביבת הקבוצה"
    body = _body(payload)
    assert body["text"] == (
        "👀 בקבוצת הוואטסאפ של {{1}} זוהתה התנהגות פוגענית.\n\n"
        "{{2}}.\n\nכדאי להיות מודעים לשיח בקבוצות."
    )
    assert not any(component["type"] == "BUTTONS" for component in components)


def test_both_template_payloads_are_available() -> None:
    payloads = template_payloads()
    assert [payload["name"] for payload in payloads] == [
        SECURITY_TEMPLATE_NAME,
        EXPOSURE_TEMPLATE_NAME,
    ]
