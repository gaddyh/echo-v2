"""Unit tests for the Gemini video summarizer adapter.

All tests mock the ``google.genai`` client and the httpx streaming
download — no network access required.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from echo_v2.services.gemini_video_summarizer import (
    DEFAULT_GEMINI_VIDEO_MAX_BYTES,
    GeminiVideoSummarizer,
    VideoTooLargeError,
)
from echo_v2.services.media_summarizer import MediaSummaryError

pytestmark = pytest.mark.asyncio


def _fake_response(text: str | None) -> Any:
    """Build a fake generate_content response object."""
    resp = MagicMock()
    resp.text = text
    return resp


def _make_stream_response(data: bytes, chunk_size: int = 4096) -> Any:
    """Build a fake httpx streaming response that yields ``data`` in chunks."""
    response = AsyncMock()
    response.raise_for_status = MagicMock()

    async def _aiter_bytes() -> Any:
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    response.aiter_bytes = _aiter_bytes
    return response


class _FakeStreamContext:
    """Async context manager that yields the fake streaming response."""

    def __init__(self, response: Any) -> None:
        self._response = response

    async def __aenter__(self) -> Any:
        return self._response

    async def __aexit__(self, *args: object) -> None:
        pass


async def test_happy_path_returns_summary() -> None:
    summarizer = GeminiVideoSummarizer(api_key="test-key")
    video_bytes = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100

    with (
        patch.object(
            httpx.AsyncClient, "stream",
            return_value=_FakeStreamContext(_make_stream_response(video_bytes)),
        ),
        patch("google.genai.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=_fake_response("סרטון קצר שמראה ילד משחק בפארק.")
        )
        mock_client_cls.return_value = mock_client

        result = await summarizer.summarize_media(
            download_url="https://media.test/video.mp4",
            mime_type="video/mp4",
            file_name="video.mp4",
            message_type="video",
            caption=None,
        )

    assert result.kind == "video"
    assert result.model == "gemini-3.8-flash"
    assert "ילד" in result.text


async def test_empty_response_raises() -> None:
    summarizer = GeminiVideoSummarizer(api_key="test-key")
    video_bytes = b"\x00" * 50

    with (
        patch.object(
            httpx.AsyncClient, "stream",
            return_value=_FakeStreamContext(_make_stream_response(video_bytes)),
        ),
        patch("google.genai.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=_fake_response("")
        )
        mock_client_cls.return_value = mock_client

        with pytest.raises(MediaSummaryError, match="empty video summary"):
            await summarizer.summarize_media(
                download_url="https://media.test/video.mp4",
                mime_type="video/mp4",
                file_name="video.mp4",
                message_type="video",
            )


async def test_none_response_raises() -> None:
    summarizer = GeminiVideoSummarizer(api_key="test-key")
    video_bytes = b"\x00" * 50

    with (
        patch.object(
            httpx.AsyncClient, "stream",
            return_value=_FakeStreamContext(_make_stream_response(video_bytes)),
        ),
        patch("google.genai.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=_fake_response(None)
        )
        mock_client_cls.return_value = mock_client

        with pytest.raises(MediaSummaryError, match="empty video summary"):
            await summarizer.summarize_media(
                download_url="https://media.test/video.mp4",
                mime_type="video/mp4",
                file_name="video.mp4",
                message_type="video",
            )


async def test_oversized_video_raises_too_large() -> None:
    # max_bytes=100, but we'll stream 500 bytes in 100-byte chunks.
    summarizer = GeminiVideoSummarizer(api_key="test-key", max_bytes=100)
    oversized = b"\x00" * 500

    with patch.object(
        httpx.AsyncClient, "stream",
        return_value=_FakeStreamContext(_make_stream_response(oversized, chunk_size=100)),
    ), pytest.raises(VideoTooLargeError, match="exceeded size cap"):
        await summarizer.summarize_media(
            download_url="https://media.test/big.mp4",
            mime_type="video/mp4",
            file_name="big.mp4",
            message_type="video",
        )


async def test_oversized_video_does_not_buffer_whole_file() -> None:
    """The streaming download should abort early — not consume all chunks."""
    summarizer = GeminiVideoSummarizer(api_key="test-key", max_bytes=50)

    consumed = 0

    class _CountingStreamResponse:
        def __init__(self) -> None:
            self.raise_for_status = MagicMock()

        async def aiter_bytes(self) -> Any:
            nonlocal consumed
            # Yield 30-byte chunks; after the second chunk (60 bytes) we
            # exceed the 50-byte cap and expect the download to abort.
            for _ in range(10):
                chunk = b"\x00" * 30
                consumed += len(chunk)
                yield chunk

    with patch.object(
        httpx.AsyncClient, "stream",
        return_value=_FakeStreamContext(_CountingStreamResponse()),
    ), pytest.raises(VideoTooLargeError):
        await summarizer.summarize_media(
            download_url="https://media.test/huge.mp4",
            mime_type="video/mp4",
            file_name="huge.mp4",
            message_type="video",
        )

    # Should have aborted after 2 chunks (60 bytes > 50), not all 10.
    assert consumed == 60, f"Expected early abort at 60 bytes, got {consumed}"


async def test_non_video_returns_unsupported() -> None:
    summarizer = GeminiVideoSummarizer(api_key="test-key")
    result = await summarizer.summarize_media(
        download_url="https://media.test/image.jpg",
        mime_type="image/jpeg",
        file_name="image.jpg",
        message_type="image",
    )
    assert result.kind == "unsupported"
    assert "image" in result.text


async def test_summarize_link_raises_not_implemented() -> None:
    summarizer = GeminiVideoSummarizer(api_key="test-key")
    with pytest.raises(NotImplementedError):
        await summarizer.summarize_link("https://example.com")


async def test_caption_passed_as_context() -> None:
    summarizer = GeminiVideoSummarizer(api_key="test-key")
    video_bytes = b"\x00" * 50

    captured_parts: list[Any] = []

    with (
        patch.object(
            httpx.AsyncClient, "stream",
            return_value=_FakeStreamContext(_make_stream_response(video_bytes)),
        ),
        patch("google.genai.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()

        async def _capture_generate(*args: Any, **kwargs: Any) -> Any:
            captured_parts.extend(kwargs.get("contents", []))
            return _fake_response("תיאור הסרטון")

        mock_client.aio.models.generate_content = _capture_generate
        mock_client_cls.return_value = mock_client

        await summarizer.summarize_media(
            download_url="https://media.test/video.mp4",
            mime_type="video/mp4",
            file_name="video.mp4",
            message_type="video",
            caption="look at this!",
        )

    # The caption should appear in one of the parts.
    caption_found = any(
        isinstance(p, str) and "look at this!" in p for p in captured_parts
    )
    assert caption_found, "Caption was not passed to Gemini as context"


async def test_default_max_bytes_is_15mb() -> None:
    assert DEFAULT_GEMINI_VIDEO_MAX_BYTES == 15 * 1024 * 1024


async def test_close_closes_owned_http_client() -> None:
    http_client = AsyncMock(spec=httpx.AsyncClient)
    summarizer = GeminiVideoSummarizer(
        api_key="test-key", http_client=http_client
    )
    await summarizer.close()
    http_client.aclose.assert_awaited_once()


async def test_close_noop_without_http_client() -> None:
    summarizer = GeminiVideoSummarizer(api_key="test-key")
    # Should not raise.
    await summarizer.close()


async def test_custom_model_used() -> None:
    summarizer = GeminiVideoSummarizer(
        api_key="test-key", model="gemini-3.5-flash"
    )
    video_bytes = b"\x00" * 50

    with (
        patch.object(
            httpx.AsyncClient, "stream",
            return_value=_FakeStreamContext(_make_stream_response(video_bytes)),
        ),
        patch("google.genai.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=_fake_response("description")
        )
        mock_client_cls.return_value = mock_client

        result = await summarizer.summarize_media(
            download_url="https://media.test/video.mp4",
            mime_type="video/mp4",
            file_name="video.mp4",
            message_type="video",
        )

    assert result.model == "gemini-3.5-flash"
