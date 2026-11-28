"""Tests for transcription helpers."""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from echo_v2.services.transcription import (
    SUPPORTED_TRANSCRIPTION_EXTS,
    MediaUnavailableError,
    TranscriptionError,
    TranscriptionResult,
    download_file,
    ensure_transcribable_audio,
    handle_direct_audio_download_url,
    safe_unlink,
    suffix_from_mime,
)

# --- suffix_from_mime ---


@pytest.mark.parametrize(
    "mime_type, expected",
    [
        ("audio/aac", ".aac"),
        ("audio/ogg", ".ogg"),
        ("audio/opus", ".ogg"),
        ("audio/mpeg", ".mpeg"),
        ("audio/mpga", ".mpga"),
        ("audio/mp4", ".mp4"),
        ("audio/wav", ".wav"),
        ("audio/webm", ".webm"),
    ],
)
def test_suffix_from_mime_known_types(mime_type: str, expected: str) -> None:
    """Known MIME types map to the expected extension."""
    assert suffix_from_mime(mime_type) == expected


def test_suffix_from_mime_unknown() -> None:
    """Unknown MIME type returns .bin."""
    assert suffix_from_mime("audio/flac") == ".bin"


def test_suffix_from_mime_strips_parameters() -> None:
    """Parameters after ; are stripped before lookup."""
    assert suffix_from_mime("audio/ogg; codec=opus") == ".ogg"


def test_suffix_from_mime_case_insensitive() -> None:
    """MIME type lookup is case-insensitive."""
    assert suffix_from_mime("AUDIO/OGG") == ".ogg"


def test_suffix_from_mime_empty_string() -> None:
    """Empty string returns .bin."""
    assert suffix_from_mime("") == ".bin"


def test_suffix_from_mime_whitespace() -> None:
    """Trailing whitespace after ; is handled."""
    assert suffix_from_mime("audio/ogg; ") == ".ogg"


# --- download_file ---


@patch("echo_v2.services.transcription.httpx.AsyncClient")
async def test_download_file_success(mock_client_cls: MagicMock, tmp_path: Path) -> None:
    """Successful download writes bytes to the target path."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b"audio data"

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    mock_instance = mock_client_cls.return_value
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    target = tmp_path / "audio.ogg"
    await download_file("http://example.com/audio.ogg", target)
    assert target.read_bytes() == b"audio data"
    mock_client.get.assert_called_once_with("http://example.com/audio.ogg")


def _mock_download_client(
    mock_client_cls: MagicMock,
    *,
    response: httpx.Response | None = None,
    get_error: Exception | None = None,
) -> AsyncMock:
    mock_client = AsyncMock()
    if get_error is not None:
        mock_client.get = AsyncMock(side_effect=get_error)
    else:
        mock_client.get = AsyncMock(return_value=response)
    mock_instance = mock_client_cls.return_value
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)
    return mock_client


@patch("echo_v2.services.transcription.httpx.AsyncClient")
async def test_download_file_http_4xx_is_unavailable_and_sanitized(
    mock_client_cls: MagicMock, tmp_path: Path
) -> None:
    """A 4xx means the remote media object is gone: terminal class, no URL leak."""
    signed = "https://connector.example/media/abc?sig=secret&exp=123"
    response = httpx.Response(403, request=httpx.Request("GET", signed))
    _mock_download_client(mock_client_cls, response=response)

    with pytest.raises(MediaUnavailableError) as excinfo:
        await download_file(signed, tmp_path / "audio.ogg")
    assert "403" in str(excinfo.value)
    assert "sig" not in str(excinfo.value)
    assert "example.com" not in str(excinfo.value)


@patch("echo_v2.services.transcription.httpx.AsyncClient")
async def test_download_file_http_5xx_stays_retryable(
    mock_client_cls: MagicMock, tmp_path: Path
) -> None:
    response = httpx.Response(503, request=httpx.Request("GET", "https://x.test/a"))
    _mock_download_client(mock_client_cls, response=response)

    with pytest.raises(TranscriptionError, match="HTTP 503"):
        await download_file("https://example.com/a.ogg", tmp_path / "a.ogg")


@patch("echo_v2.services.transcription.httpx.AsyncClient")
async def test_download_file_transport_error_sanitized(
    mock_client_cls: MagicMock, tmp_path: Path
) -> None:
    _mock_download_client(
        mock_client_cls, get_error=httpx.ConnectError("refused https://signed.url/x")
    )
    with pytest.raises(TranscriptionError, match="ConnectError") as excinfo:
        await download_file("https://signed.url/x", tmp_path / "a.ogg")
    assert "signed.url" not in str(excinfo.value)


# --- ensure_transcribable_audio ---


@pytest.mark.parametrize("ext", sorted(SUPPORTED_TRANSCRIPTION_EXTS))
def test_ensure_transcribable_audio_supported_ext(ext: str, tmp_path: Path) -> None:
    """Supported extensions return the path unchanged."""
    path = tmp_path / f"audio{ext}"
    path.write_bytes(b"data")
    result = ensure_transcribable_audio(path)
    assert result == path


@patch("echo_v2.services.transcription.subprocess.run")
def test_ensure_transcribable_audio_unsupported_ext(
    mock_run: MagicMock, tmp_path: Path
) -> None:
    """Unsupported extension triggers ffmpeg conversion to .wav."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    path = tmp_path / "audio.amr"
    path.write_bytes(b"data")
    result = ensure_transcribable_audio(path)
    assert result == path.with_suffix(".wav")
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == "ffmpeg"
    assert str(path) in args


@patch("echo_v2.services.transcription.subprocess.run")
def test_ensure_transcribable_audio_ffmpeg_failure(
    mock_run: MagicMock, tmp_path: Path
) -> None:
    """ffmpeg failure raises RuntimeError."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "conversion error"
    mock_run.return_value = mock_result

    path = tmp_path / "audio.bin"
    path.write_bytes(b"data")
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        ensure_transcribable_audio(path)


# --- safe_unlink ---


def test_safe_unlink_existing_file(tmp_path: Path) -> None:
    """Existing file is deleted."""
    path = tmp_path / "test.txt"
    path.write_text("hello")
    assert path.exists()
    safe_unlink(path)
    assert not path.exists()


def test_safe_unlink_nonexistent_file(tmp_path: Path) -> None:
    """Non-existent file does not raise."""
    path = tmp_path / "nonexistent.txt"
    assert not path.exists()
    safe_unlink(path)


def test_safe_unlink_real_temp_file() -> None:
    """Real NamedTemporaryFile is deleted by safe_unlink."""
    with NamedTemporaryFile(delete=False) as f:
        path = Path(f.name)
    assert path.exists()
    safe_unlink(path)
    assert not path.exists()


# --- handle_direct_audio_download_url ---


class FakeTranscriber:
    """A fake Transcriber implementing the Protocol."""

    def __init__(self, text: str = "transcribed text") -> None:
        self._text = text
        self.closed = False

    async def transcribe_bytes(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
        child_user_id: str | None = None,
    ) -> TranscriptionResult:
        return TranscriptionResult(text=self._text, language="en", model="fake")

    async def close(self) -> None:
        self.closed = True


@patch("echo_v2.services.transcription.ensure_transcribable_audio")
@patch("echo_v2.services.transcription.download_file", new_callable=AsyncMock)
async def test_handle_direct_audio_download_url_success(
    mock_download: AsyncMock, mock_ensure: MagicMock
) -> None:
    """Full pipeline: download → convert → transcribe → return text."""
    mock_download.return_value = None
    mock_path = MagicMock()
    mock_path.read_bytes.return_value = b"audio data"
    mock_path.name = "audio.wav"
    mock_ensure.return_value = mock_path

    transcriber = FakeTranscriber(text="hello world")
    result = await handle_direct_audio_download_url(
        transcriber=transcriber,
        download_url="http://example.com/audio.ogg",
        mime_type="audio/ogg",
    )
    assert result == "hello world"
    mock_download.assert_called_once()


@patch("echo_v2.services.transcription.download_file", new_callable=AsyncMock)
async def test_handle_direct_audio_download_url_download_failure(
    mock_download: AsyncMock,
) -> None:
    """Download failure propagates the exception."""
    mock_download.side_effect = RuntimeError("download failed")

    transcriber = FakeTranscriber()
    with pytest.raises(RuntimeError, match="download failed"):
        await handle_direct_audio_download_url(
            transcriber=transcriber,
            download_url="http://example.com/audio.ogg",
            mime_type="audio/ogg",
        )


@patch("echo_v2.services.transcription.ensure_transcribable_audio")
@patch("echo_v2.services.transcription.download_file", new_callable=AsyncMock)
async def test_handle_direct_audio_download_url_transcriber_failure(
    mock_download: AsyncMock, mock_ensure: MagicMock
) -> None:
    """Transcriber failure propagates the exception."""
    mock_download.return_value = None
    mock_path = MagicMock()
    mock_path.read_bytes.return_value = b"audio data"
    mock_path.name = "audio.wav"
    mock_ensure.return_value = mock_path

    transcriber = AsyncMock()
    transcriber.transcribe_bytes = AsyncMock(
        side_effect=RuntimeError("transcribe failed")
    )

    with pytest.raises(RuntimeError, match="transcribe failed"):
        await handle_direct_audio_download_url(
            transcriber=transcriber,
            download_url="http://example.com/audio.ogg",
            mime_type="audio/ogg",
        )


def test_safe_unlink_os_error_is_swallowed(tmp_path: Path) -> None:
    """OSError from unlink is caught and swallowed (best-effort cleanup)."""
    from unittest.mock import patch

    path = tmp_path / "test.txt"
    path.write_text("hello")

    with patch("pathlib.Path.unlink", side_effect=OSError("permission denied")):
        # Should not raise.
        safe_unlink(path)
