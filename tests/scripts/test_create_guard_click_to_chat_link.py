"""Tests for the Echo Guard Click-to-Chat invitation helper."""

from __future__ import annotations

import pytest

from scripts.create_guard_click_to_chat_link import build_click_to_chat_url


def test_build_click_to_chat_url_encodes_join_message() -> None:
    url = build_click_to_chat_url("0546610653")
    assert url.startswith("https://wa.me/972546610653?text=")
    assert "%D7%94%D7%99%D7%99" in url
    assert "אקו" not in url


def test_build_click_to_chat_url_rejects_invalid_phone() -> None:
    with pytest.raises(ValueError, match="invalid ECHO_BOT_PHONE"):
        build_click_to_chat_url("not-a-phone")
