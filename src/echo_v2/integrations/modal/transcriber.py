"""Adapter that implements the Transcriber protocol via ModalTranscriptionClient.

Ported from tami_one's ``modal_transcriber.py``. Translates transport-specific
errors into the provider-neutral ``TranscriptionError`` and maps raw JSON into
``TranscriptionResult``.
"""

from __future__ import annotations

import logging

from echo_v2.integrations.modal.client import (
    ModalTranscriptionClient,
    ModalTranscriptionTransportError,
)
from echo_v2.integrations.modal.settings import ModalTranscriptionSettings
from echo_v2.services.transcription import (
    TranscriptionError,
    TranscriptionResult,
)

__all__ = ["ModalWhisperTranscriber"]

_logger = logging.getLogger("echo_v2.integrations.modal.transcriber")


class ModalWhisperTranscriber:
    """Adapter implementing the :class:`Transcriber` protocol via Modal.

    Delegates to :class:`ModalTranscriptionClient` (raw HTTP transport).
    Translates transport-specific errors into the provider-neutral
    :class:`TranscriptionError` and maps raw JSON into :class:`TranscriptionResult`.
    """

    def __init__(
        self,
        settings: ModalTranscriptionSettings,
        *,
        http_client=None,
    ) -> None:
        self._client = ModalTranscriptionClient(
            settings,
            http_client=http_client,
        )

    async def transcribe_bytes(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
    ) -> TranscriptionResult:
        try:
            payload = await self._client.transcribe_bytes(
                audio_bytes=audio_bytes,
                filename=filename,
                content_type=content_type,
            )
        except ModalTranscriptionTransportError as exc:
            _logger.error("Modal transcription failed: %s", exc)
            raise TranscriptionError(str(exc)) from exc

        text = payload.get("text")

        if not isinstance(text, str):
            raise TranscriptionError(
                "Modal response does not contain a valid text field"
            )

        return TranscriptionResult(
            text=text.strip(),
            language=payload.get("language"),
            audio_duration_seconds=payload.get("audio_duration_seconds"),
            processing_seconds=payload.get("processing_seconds"),
            model=str(payload.get("model", "unknown")),
            raw=payload,
        )

    async def close(self) -> None:
        await self._client.close()
