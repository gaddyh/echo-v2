"""Adapter that implements the Transcriber protocol via ModalTranscriptionClient.

Ported from tami_one's ``modal_transcriber.py``. Translates transport-specific
errors into the provider-neutral ``TranscriptionError`` and maps raw JSON into
``TranscriptionResult``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import httpx

from echo_v2.domain.ai_usage_event import AiUsageStatus, AiUsageType
from echo_v2.integrations.modal.client import (
    ModalTranscriptionClient,
    ModalTranscriptionTransportError,
)
from echo_v2.integrations.modal.settings import ModalTranscriptionSettings
from echo_v2.services.transcription import (
    TranscriptionError,
    TranscriptionResult,
)

if TYPE_CHECKING:
    from echo_v2.services.reporting.ai_usage_recorder import AiUsageRecorder

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
        http_client: httpx.AsyncClient | None = None,
        recorder: AiUsageRecorder | None = None,
    ) -> None:
        self._client = ModalTranscriptionClient(
            settings,
            http_client=http_client,
        )
        self._recorder = recorder

    async def transcribe_bytes(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
        child_user_id: str | None = None,
    ) -> TranscriptionResult:
        invocation_id = uuid4()
        try:
            payload = await self._client.transcribe_bytes(
                audio_bytes=audio_bytes,
                filename=filename,
                content_type=content_type,
            )
        except ModalTranscriptionTransportError as exc:
            _logger.error("Modal transcription failed: %s", exc)
            await self._record_usage(
                child_user_id=child_user_id,
                status=AiUsageStatus.ERROR,
                invocation_id=invocation_id,
                error_type=type(exc).__name__,
            )
            raise TranscriptionError(str(exc)) from exc

        text = payload.get("text")

        if not isinstance(text, str):
            await self._record_usage(
                child_user_id=child_user_id,
                status=AiUsageStatus.ERROR,
                invocation_id=invocation_id,
                error_type="InvalidPayload",
            )
            raise TranscriptionError(
                "Modal response does not contain a valid text field"
            )

        result = TranscriptionResult(
            text=text.strip(),
            language=payload.get("language"),
            audio_duration_seconds=payload.get("audio_duration_seconds"),
            processing_seconds=payload.get("processing_seconds"),
            model=str(payload.get("model", "unknown")),
            raw=payload,
        )
        await self._record_usage(
            child_user_id=child_user_id,
            status=AiUsageStatus.SUCCESS,
            invocation_id=invocation_id,
            audio_duration_seconds=result.audio_duration_seconds,
            processing_seconds=result.processing_seconds,
            model=result.model,
        )
        return result

    async def close(self) -> None:
        await self._client.close()

    async def _record_usage(
        self,
        *,
        child_user_id: str | None,
        status: str,
        invocation_id: UUID,
        audio_duration_seconds: float | None = None,
        processing_seconds: float | None = None,
        model: str = "unknown",
        error_type: str | None = None,
    ) -> None:
        """Record one billable Modal provider attempt. Best-effort."""
        if self._recorder is None:
            return
        from echo_v2.services.reporting.ai_usage_recorder import (
            UsageContext,
            UsagePayload,
        )

        usage_details: dict[str, object] = {}
        if audio_duration_seconds is not None:
            usage_details["audio_duration_seconds"] = audio_duration_seconds
        if processing_seconds is not None:
            usage_details["processing_seconds"] = processing_seconds

        context = UsageContext(
            operation="media_audio",
            provider="modal",
            model=model,
            child_user_id=child_user_id,
        )
        payload = UsagePayload(
            usage_type=AiUsageType.AUDIO_SECONDS,
            status=status,
            usage_details=usage_details,
            invocation_id=invocation_id,
            error_type=error_type if status == AiUsageStatus.ERROR else None,
        )
        await self._recorder.record(context, payload)
