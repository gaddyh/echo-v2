"""Tests for DigestFormatter (template parameter mode)."""

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


def test_format_single_item_with_name_and_text():
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
    assert 'דנה: "יש מצב לחמישי?"' in params.items_text


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
    assert 'דנה: "יש מצב לחמישי?"' in params.items_text
    assert 'יוסי: "תשלח לי את ההצעה?"' in params.items_text


def test_format_sorts_by_waiting_since_ascending():
    """Oldest waiting_since first."""
    formatter = DigestFormatter()
    items = [
        DigestItem(
            active=_make_active(chat_id="972508765432@c.us", waiting_since=LATER),
            contact_name="יוסי",
            last_message_text="תשלח לי את ההצעה?",
        ),
        DigestItem(
            active=_make_active(chat_id="972501234567@c.us", waiting_since=NOW),
            contact_name="דנה",
            last_message_text="יש מצב לחמישי?",
        ),
    ]
    params = formatter.format(items, first_name="גדי")
    # דנה (NOW) should appear before יוסי (LATER)
    dana_pos = params.items_text.index("דנה")
    yossi_pos = params.items_text.index("יוסי")
    assert dana_pos < yossi_pos


def test_format_falls_back_to_phone_when_no_name():
    formatter = DigestFormatter()
    items = [
        DigestItem(
            active=_make_active(chat_id="972501234567@c.us"),
            contact_name=None,
            last_message_text="יש מצב לחמישי?",
        ),
    ]
    params = formatter.format(items, first_name="גדי")
    assert '972501234567: "יש מצב לחמישי?"' in params.items_text


def test_format_falls_back_for_media_only():
    formatter = DigestFormatter()
    items = [
        DigestItem(
            active=_make_active(),
            contact_name="דנה",
            last_message_text=None,
        ),
    ]
    params = formatter.format(items, first_name="גדי")
    assert "שלח/ה הודעה שמחכה להתייחסותך" in params.items_text


def test_format_truncates_at_20_items():
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
    assert "ועוד 5 שיחות..." in params.items_text
