"""Provider-neutral transcription port and audio processing helpers.

Ported from tami_one's transcription module, adapted to echo-v2 conventions:
- No OpenAITranscriber (Modal-only for now; can add later).
- No 360dialog-specific helpers (echo-v2 uses the generic
  ``handle_direct_audio_download_url`` for Green API audio).
- No langsmith wrapping (echo-v2 handles tracing at the caller boundary).

The ``Transcriber`` protocol is the boundary between the application and any
transcription provider. The ``handle_direct_audio_download_url`` helper is the
full pipeline: download → ffmpeg-convert → transcribe → return text.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

__all__ = [
    "SUPPORTED_TRANSCRIPTION_EXTS",
    "Transcriber",
    "TranscriptionError",
    "TranscriptionResult",
    "download_file",
    "ensure_transcribable_audio",
    "handle_direct_audio_download_url",
    "safe_unlink",
    "suffix_from_mime",
]

_logger = logging.getLogger("echo_v2.services.transcription")

SUPPORTED_TRANSCRIPTION_EXTS = {
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".m4a",
    ".wav",
    ".webm",
    ".ogg",
    ".opus",
}


def suffix_from_mime(mime_type: str) -> str:
    """Map a MIME type to a file extension.

    Returns ``.bin`` for unknown types.
    """
    normalized = mime_type.split(";")[0].strip().lower()

    mapping = {
        "audio/aac": ".aac",
        "audio/amr": ".amr",
        "audio/ogg": ".ogg",
        "audio/opus": ".ogg",
        "audio/mpeg": ".mpeg",
        "audio/mpga": ".mpga",
        "audio/mp4": ".mp4",
        "audio/wav": ".wav",
        "audio/webm": ".webm",
    }

    return mapping.get(normalized, ".bin")


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str | None = None
    audio_duration_seconds: float | None = None
    processing_seconds: float | None = None
    model: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict)


class TranscriptionError(RuntimeError):
    """Provider-neutral transcription failure."""


@runtime_checkable
class Transcriber(Protocol):
    async def transcribe_bytes(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
    ) -> TranscriptionResult: ...

    async def close(self) -> None: ...


async def handle_direct_audio_download_url(
    *,
    transcriber: Transcriber,
    download_url: str,
    file_name: str = "voice-message",
    mime_type: str = "",
) -> str:
    """Download audio from a direct URL, convert if needed, and transcribe.

    This is the generic pipeline for providers that give a direct download URL
    (e.g. Green API's ``fileMessageData.downloadUrl``). Downloads to a temp
    file, converts to a transcribable format via ffmpeg if needed, then calls
    the transcriber.

    Returns the transcribed text.
    """
    suffix = Path(file_name).suffix or suffix_from_mime(mime_type)

    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = Path(tmpdir) / f"audio{suffix}"
        await download_file(download_url, raw_path)
        transcribable_path = ensure_transcribable_audio(raw_path)
        audio_bytes = transcribable_path.read_bytes()
        result = await transcriber.transcribe_bytes(
            audio_bytes=audio_bytes,
            filename=transcribable_path.name,
            content_type=mime_type,
        )
        _logger.info(
            "transcription complete: provider=%s model=%s language=%s "
            "audio_bytes=%d audio_duration=%.2fs processing=%.2fs text=%s",
            type(transcriber).__name__,
            result.model,
            result.language,
            len(audio_bytes),
            result.audio_duration_seconds or 0.0,
            result.processing_seconds or 0.0,
            result.text[:200],
        )
        return result.text


async def download_file(url: str, target_path: Path) -> None:
    """Download a URL to a local path using httpx."""
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        target_path.write_bytes(response.content)


def ensure_transcribable_audio(path: Path) -> Path:
    """Ensure the audio file is in a transcribable format.

    If the file extension is already supported, return the path unchanged.
    Otherwise, convert to 16kHz mono WAV via ffmpeg and return the new path.
    """
    if path.suffix.lower() in SUPPORTED_TRANSCRIPTION_EXTS:
        return path

    converted = path.with_suffix(".wav")

    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(path),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(converted),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr}")

    return converted


def safe_unlink(path: Path) -> None:
    """Unlink a file, ignoring errors."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
