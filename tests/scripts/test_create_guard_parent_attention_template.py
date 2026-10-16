"""Tests for the no-link Baseline B WhatsApp template."""

from __future__ import annotations

from scripts.create_guard_parent_attention_template import template_payload


def test_parent_attention_template_has_situation_placeholders_and_no_url_button() -> None:
    payload = template_payload()
    components = payload["components"]
    assert isinstance(components, list)
    body = next(component for component in components if component["type"] == "BODY")
    assert "{{1}}" in body["text"]
    assert "{{2}}" in body["text"]
    assert "{{3}}" in body["text"]
    assert not any(component["type"] == "BUTTONS" for component in components)
