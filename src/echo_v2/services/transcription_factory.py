"""Composition root for the Transcriber.

This is the only place that decides which transcription provider to use.
Callers receive a ``Transcriber | None`` and should not know which
implementation they got (or whether transcription is enabled at all).
"""

from __future__ import annotations

import logging

from echo_v2.integrations.modal.settings import (
    is_configured,
    load_settings,
)
from echo_v2.services.transcription import Transcriber

__all__ = ["build_transcriber"]

_logger = logging.getLogger("echo_v2.services.transcription_factory")


def build_transcriber() -> Transcriber | None:
    """Build a Transcriber from environment settings.

    Currently supports only the Modal Hebrew Whisper endpoint. If Modal is
    not configured (missing env vars), returns ``None`` — callers should treat
    this as "transcription disabled" and proceed without transcription.

    Returns ``None`` if the Modal settings are incomplete.
    """
    settings = load_settings()

    if not is_configured(settings):
        _logger.info(
            "Modal transcription not configured (MODAL_TRANSCRIPTION_URL/KEY/SECRET "
            "not set) — transcription disabled"
        )
        return None

    # Lazy import so the Modal client is only loaded when needed.
    from echo_v2.integrations.modal.transcriber import ModalWhisperTranscriber

    _logger.info("Modal transcription enabled: endpoint=%s", settings.endpoint_url)
    return ModalWhisperTranscriber(settings)
