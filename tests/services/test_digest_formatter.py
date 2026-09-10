"""Tests for DigestFormatter (template parameter mode).

The template now produces only 2 params (first_name, count) — no
conversation content is exposed in the template. The interactive list
is sent only after the user taps the button.
"""

from __future__ import annotations

from datetime import datetime, timezone

from echo_v2.domain.waiting_for_me import WaitingForMeActive
from echo_v2.services.digest_formatter import DigestFormatter, DigestItem

NOW = datetime(2026, 9, 12, 8, 0, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)


def _make_active(chat_id="972501234567@c.us", waiting_since=NOW, target_version=1):
    return WaitingForMeActive(
        user_id="user-1",
        chat_id=chat_id,
        target_version=target_version,
        result_id="result-1",
        waiting_since=waiting_since,
    )


def test_format_single_item():
    formatter = DigestFormatter()
    items = [
        DigestItem(
            active=_make_active(),
            contact_name="דנה",
            last_message_text="יש מצב לחמישי?",
        ),
    ]
    params = formatter.format(items, first_name="גדי")
    assert params.first_name == "גדי"
    assert params.count == "1"


def test_format_multiple_items():
    formatter = DigestFormatter()
    items = [
        DigestItem(
            active=_make_active(chat_id="972501234567@c.us", waiting_since=NOW),
            contact_name="דנה",
            last_message_text="יש מצב לחמישי?",
        ),
        DigestItem(
            active=_make_active(chat_id="972508765432@c.us", waiting_since=LATER),
            contact_name="יוסי",
            last_message_text="תשלח לי את ההצעה?",
        ),
    ]
    params = formatter.format(items, first_name="גדי")
    assert params.count == "2"


def test_format_no_items():
    """Empty list produces count=0."""
    formatter = DigestFormatter()
    params = formatter.format([], first_name="גדי")
    assert params.first_name == "גדי"
    assert params.count == "0"


def test_format_25_items():
    """25 items produce count=25 (no truncation in the count)."""
    formatter = DigestFormatter()
    items = [
        DigestItem(
            active=_make_active(chat_id=f"97250{i:07d}@c.us", waiting_since=NOW),
            contact_name=f"contact-{i}",
            last_message_text=f"msg-{i}",
        )
        for i in range(25)
    ]
    params = formatter.format(items, first_name="גדי")
    assert params.count == "25"
