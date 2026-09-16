"""Settings for the Modal Hebrew Whisper transcription endpoint.

Loaded from environment variables. Required for any Modal transcription
operation; the pure runtime does not import this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["ModalTranscriptionSettings", "load_settings"]


@dataclass(frozen=True)
class ModalTranscriptionSettings:
    """Configuration for the Modal Hebrew Whisper transcription endpoint."""

    endpoint_url: str
    key: str
    secret: str
    timeout_seconds: float = 180.0


def load_settings(
    *,
    endpoint_url: str | None = None,
    key: str | None = None,
    secret: str | None = None,
    timeout_seconds: float | None = None,
) -> ModalTranscriptionSettings:
    """Build :class:`ModalTranscriptionSettings` from arguments or environment.

    Reads ``MODAL_TRANSCRIPTION_URL``, ``MODAL_TRANSCRIPTION_KEY``,
    ``MODAL_TRANSCRIPTION_SECRET``, and ``MODAL_TRANSCRIPTION_TIMEOUT_SECONDS``.
    Missing values default to empty strings (use :meth:`is_configured` to check).
    """
    return ModalTranscriptionSettings(
        endpoint_url=(endpoint_url or os.getenv("MODAL_TRANSCRIPTION_URL", "")).strip(),
        key=(key or os.getenv("MODAL_TRANSCRIPTION_KEY", "")).strip(),
        secret=(secret or os.getenv("MODAL_TRANSCRIPTION_SECRET", "")).strip(),
        timeout_seconds=float(
            timeout_seconds
            if timeout_seconds is not None
            else os.getenv("MODAL_TRANSCRIPTION_TIMEOUT_SECONDS", "180")
        ),
    )


def is_configured(settings: ModalTranscriptionSettings) -> bool:
    """Return True only if all required fields are set."""
    return bool(settings.endpoint_url and settings.key and settings.secret)
