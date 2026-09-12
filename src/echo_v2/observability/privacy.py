"""Privacy helpers for observability — HMAC-hashing of IDs for trace metadata.

When LangSmith tracing is enabled, raw user IDs and chat IDs must never
appear in trace metadata. Phone numbers are enumerable, so plain SHA-256
is not sufficient — an attacker could enumerate numbers and compare
hashes. A keyed HMAC prevents this.

The key is read from ``OBSERVABILITY_HASH_KEY``. When tracing is
enabled (``LANGSMITH_TRACING=true``), the key is required — startup
fails without it. When tracing is disabled, :func:`correlation_id` is
not called and the key is not needed.
"""

from __future__ import annotations

import hashlib
import hmac
import os

__all__ = ["correlation_id", "ensure_hash_key_or_fail"]

_HASH_KEY: bytes | None = None


def _get_key() -> bytes:
    global _HASH_KEY
    if _HASH_KEY is None:
        key = os.environ.get("OBSERVABILITY_HASH_KEY")
        if not key:
            raise RuntimeError(
                "OBSERVABILITY_HASH_KEY is required when tracing is enabled"
            )
        _HASH_KEY = key.encode()
    return _HASH_KEY


def correlation_id(value: str) -> str:
    """HMAC-SHA256 of an ID for trace metadata. Not reversible.

    Returns a 24-character hex digest. The key is read from
    ``OBSERVABILITY_HASH_KEY``. Raises ``RuntimeError`` if the key is
    missing — this should be caught at startup, not at trace time.
    """
    digest = hmac.new(_get_key(), value.encode(), hashlib.sha256).hexdigest()
    return digest[:24]


def ensure_hash_key_or_fail() -> None:
    """Validate that OBSERVABILITY_HASH_KEY is set when tracing is enabled.

    Call this at application startup. Raises ``RuntimeError`` if
    ``LANGSMITH_TRACING=true`` but ``OBSERVABILITY_HASH_KEY`` is missing.
    """
    tracing = os.environ.get("LANGSMITH_TRACING", "false").lower() in (
        "1",
        "true",
        "yes",
    )
    if tracing and not os.environ.get("OBSERVABILITY_HASH_KEY"):
        raise RuntimeError(
            "OBSERVABILITY_HASH_KEY is required when LANGSMITH_TRACING=true"
        )


def _reset_key_cache() -> None:
    """Reset the cached key — for tests only."""
    global _HASH_KEY
    _HASH_KEY = None
