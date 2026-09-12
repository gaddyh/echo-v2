"""Tests for observability privacy helpers — HMAC correlation IDs and sanitizers.

These tests verify:
- HMAC correlation IDs are deterministic and keyed.
- Different inputs produce different hashes.
- ``ensure_hash_key_or_fail`` raises when tracing is enabled without a key.
- Sanitizers strip PII (user_id, chat_id, message text) and keep only
  safe correlation fields.
- Sanitizers do not leak phone numbers or chat IDs into trace metadata.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from echo_v2.domain.chat import ChatState
from echo_v2.domain.waiting_for_me import (
    WaitingForMeDecision,
    WaitingForMeResult,
)
from echo_v2.observability.privacy import (
    _reset_key_cache,
    correlation_id,
    ensure_hash_key_or_fail,
)
from echo_v2.services.chat_analysis_worker import (
    ConversationInput,
    safe_process_chat_inputs,
    safe_process_chat_output,
)
from echo_v2.services.waiting_for_me_analyzer import (
    WFM_PROMPT_VERSION,
    safe_analysis_inputs,
    safe_analysis_output,
)
from tests.services.test_waiting_for_me_analyzer import _make_conversation

# --- HMAC correlation_id ----------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_key():
    """Reset the cached hash key before each test."""
    _reset_key_cache()
    yield
    _reset_key_cache()


def test_correlation_id_is_deterministic(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_HASH_KEY", "test-key-12345")
    h1 = correlation_id("user-abc")
    h2 = correlation_id("user-abc")
    assert h1 == h2


def test_correlation_id_different_inputs_different_hashes(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_HASH_KEY", "test-key-12345")
    h1 = correlation_id("user-abc")
    h2 = correlation_id("user-def")
    assert h1 != h2


def test_correlation_id_different_keys_different_hashes(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_HASH_KEY", "key-A")
    h1 = correlation_id("user-abc")
    _reset_key_cache()
    monkeypatch.setenv("OBSERVABILITY_HASH_KEY", "key-B")
    h2 = correlation_id("user-abc")
    assert h1 != h2


def test_correlation_id_is_24_chars(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_HASH_KEY", "test-key-12345")
    h = correlation_id("user-abc")
    assert len(h) == 24
    # All hex
    assert all(c in "0123456789abcdef" for c in h)


def test_correlation_id_raises_without_key(monkeypatch):
    monkeypatch.delenv("OBSERVABILITY_HASH_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OBSERVABILITY_HASH_KEY is required"):
        correlation_id("user-abc")


def test_correlation_id_does_not_reveal_input(monkeypatch):
    """The hash should not contain the input string."""
    monkeypatch.setenv("OBSERVABILITY_HASH_KEY", "test-key-12345")
    phone = "972501234567"
    h = correlation_id(phone)
    assert phone not in h
    assert h != phone


# --- ensure_hash_key_or_fail ------------------------------------------------


def test_ensure_hash_key_passes_when_tracing_disabled(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.delenv("OBSERVABILITY_HASH_KEY", raising=False)
    # Should not raise
    ensure_hash_key_or_fail()


def test_ensure_hash_key_fails_when_tracing_enabled_without_key(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("OBSERVABILITY_HASH_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OBSERVABILITY_HASH_KEY is required"):
        ensure_hash_key_or_fail()


def test_ensure_hash_key_passes_when_tracing_enabled_with_key(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("OBSERVABILITY_HASH_KEY", "test-key-12345")
    ensure_hash_key_or_fail()


def test_ensure_hash_key_tracing_enabled_variants(monkeypatch):
    for val in ("1", "true", "yes"):
        monkeypatch.setenv("LANGSMITH_TRACING", val)
        monkeypatch.delenv("OBSERVABILITY_HASH_KEY", raising=False)
        with pytest.raises(RuntimeError):
            ensure_hash_key_or_fail()


# --- safe_analysis_inputs ---------------------------------------------------


def test_safe_analysis_inputs_strips_pii(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_HASH_KEY", "test-key-12345")
    conv = _make_conversation(
        messages=[("inbound", "My phone is 972501234567")],
    )
    inputs = {"self": MagicMock(), "conversation": conv}
    sanitized = safe_analysis_inputs(inputs)

    # No raw user_id, chat_id, or message text
    assert "user-1" not in str(sanitized)
    assert "972501234567@c.us" not in str(sanitized)
    assert "972501234567" not in str(sanitized)
    assert "My phone is" not in str(sanitized)
    assert "self" not in sanitized

    # Safe fields present
    assert "user_id_hash" in sanitized
    assert "chat_id_hash" in sanitized
    assert "target_version" in sanitized
    assert "message_count" in sanitized
    assert sanitized["target_version"] == 1
    assert sanitized["message_count"] == 1
    assert sanitized["prompt_version"] == WFM_PROMPT_VERSION


def test_safe_analysis_inputs_hashes_are_consistent(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_HASH_KEY", "test-key-12345")
    conv = _make_conversation()
    inputs1 = {"conversation": conv}
    inputs2 = {"conversation": conv}
    s1 = safe_analysis_inputs(inputs1)
    s2 = safe_analysis_inputs(inputs2)
    assert s1["user_id_hash"] == s2["user_id_hash"]
    assert s1["chat_id_hash"] == s2["chat_id_hash"]


def test_safe_analysis_inputs_no_conversation_returns_prompt_version(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_HASH_KEY", "test-key-12345")
    sanitized = safe_analysis_inputs({})
    assert sanitized == {"prompt_version": WFM_PROMPT_VERSION}


# --- safe_analysis_output ---------------------------------------------------


def test_safe_analysis_output_strips_reason_and_summary():
    """Output sanitizer keeps decision+confidence+version, strips reason/summary."""
    result = WaitingForMeResult(
        decision=WaitingForMeDecision.WAITING_FOR_ME,
        confidence=0.9,
        reason="User mentioned phone 972501234567",
        summary="מחכה לאישור פגישה",
        target_version=3,
    )
    sanitized = safe_analysis_output(result)

    assert sanitized["decision"] == "waiting_for_me"
    assert sanitized["confidence"] == 0.9
    assert sanitized["target_version"] == 3
    # reason and summary are NOT in the sanitized output
    assert "reason" not in sanitized
    assert "summary" not in sanitized
    assert "972501234567" not in str(sanitized)
    assert "מחכה" not in str(sanitized)


# --- safe_process_chat_inputs ------------------------------------------------


def test_safe_process_chat_inputs_strips_pii(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_HASH_KEY", "test-key-12345")
    from datetime import datetime, timezone

    chat = ChatState(
        user_id="user-1",
        chat_id="972501234567@c.us",
        activity_version=5,
        last_message_at=datetime.now(timezone.utc),
        last_direction="inbound",
        next_analysis_at=None,
        last_processed_version=0,
    )
    inputs = {"self": MagicMock(), "chat": chat}
    sanitized = safe_process_chat_inputs(inputs)

    assert "user-1" not in str(sanitized)
    assert "972501234567@c.us" not in str(sanitized)
    assert "self" not in sanitized

    assert "user_id_hash" in sanitized
    assert "chat_id_hash" in sanitized
    assert sanitized["target_version"] == 5


def test_safe_process_chat_inputs_no_chat_returns_empty():
    sanitized = safe_process_chat_inputs({})
    assert sanitized == {}


# --- safe_process_chat_output ------------------------------------------------


def test_safe_process_chat_output_returns_status():
    assert safe_process_chat_output("committed") == {"status": "committed"}
    assert safe_process_chat_output("stale") == {"status": "stale"}
    assert safe_process_chat_output("missing") == {"status": "missing"}


# --- Transparency: no PII in any sanitized output ----------------------------


def test_no_phone_numbers_in_sanitized_metadata(monkeypatch):
    """Regression: phone numbers must never appear in sanitized trace metadata."""
    monkeypatch.setenv("OBSERVABILITY_HASH_KEY", "test-key-12345")
    phone = "972501234567"
    conv = ConversationInput(
        user_id=f"user-{phone}",
        chat_id=f"{phone}@c.us",
        target_version=1,
        messages=[("inbound", f"Call me at {phone}")],
    )
    inputs = {"conversation": conv}
    sanitized = safe_analysis_inputs(inputs)
    serialized = str(sanitized)
    assert phone not in serialized
    assert f"{phone}@c.us" not in serialized
    assert "Call me at" not in serialized


def test_no_message_text_in_sanitized_metadata(monkeypatch):
    """Regression: message text must never appear in sanitized trace metadata."""
    monkeypatch.setenv("OBSERVABILITY_HASH_KEY", "test-key-12345")
    secret_text = "this is a secret message"
    conv = ConversationInput(
        user_id="user-1",
        chat_id="chat-1@c.us",
        target_version=1,
        messages=[("inbound", secret_text)],
    )
    inputs = {"conversation": conv}
    sanitized = safe_analysis_inputs(inputs)
    assert secret_text not in str(sanitized)
