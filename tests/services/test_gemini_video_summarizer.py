"""Unit tests for the Gemini video summarizer adapter.

All tests mock the ``google.genai`` client and the httpx streaming
download — no network access required.
"""
from __future__ import annotations

import socket
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
    response.status_code = 200
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
        patch.object(GeminiVideoSummarizer, "_assert_safe_url"),
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
    assert result.model == "gemini-3.5-flash-lite"
    assert "ילד" in result.text


async def test_empty_response_raises() -> None:
    summarizer = GeminiVideoSummarizer(api_key="test-key")
    video_bytes = b"\x00" * 50

    with (
        patch.object(
            httpx.AsyncClient, "stream",
            return_value=_FakeStreamContext(_make_stream_response(video_bytes)),
        ),
        patch.object(GeminiVideoSummarizer, "_assert_safe_url"),
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
        patch.object(GeminiVideoSummarizer, "_assert_safe_url"),
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
    ), patch.object(GeminiVideoSummarizer, "_assert_safe_url"), pytest.raises(VideoTooLargeError, match="exceeded size cap"):
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
            self.status_code = 200
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
    ), patch.object(GeminiVideoSummarizer, "_assert_safe_url"), pytest.raises(VideoTooLargeError):
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
        patch.object(GeminiVideoSummarizer, "_assert_safe_url"),
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
        patch.object(GeminiVideoSummarizer, "_assert_safe_url"),
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


# --- YouTube summarization tests -------------------------------------------


async def test_youtube_happy_path_returns_summary() -> None:
    summarizer = GeminiVideoSummarizer(api_key="test-key")
    captured_contents: list[Any] = []

    with patch("google.genai.Client") as mock_client_cls:
        mock_client = MagicMock()

        async def _capture_generate(*args: Any, **kwargs: Any) -> Any:
            captured_contents.append(kwargs.get("contents"))
            return _fake_response("סרטון יוטיוב עם ילדים")

        mock_client.aio.models.generate_content = _capture_generate
        mock_client_cls.return_value = mock_client

        result = await summarizer.summarize_youtube("https://www.youtube.com/watch?v=abc123")

    assert result.kind == "video"
    assert result.model == "gemini-3.5-flash-lite"
    assert "יוטיוב" in result.text
    # The contents should be a Content with a Part carrying file_data + video_metadata.
    content = captured_contents[0]
    from google.genai import types as _types
    assert isinstance(content, _types.Content)
    assert len(content.parts) == 2
    video_part = content.parts[0]
    assert isinstance(video_part, _types.Part)
    assert video_part.file_data.file_uri == "https://www.youtube.com/watch?v=abc123"
    assert video_part.video_metadata.start_offset == "0s"
    assert video_part.video_metadata.end_offset == "60s"
    # The second part should carry the prompt text.
    assert content.parts[1].text is not None


async def test_youtube_empty_response_raises() -> None:
    summarizer = GeminiVideoSummarizer(api_key="test-key")

    with patch("google.genai.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=_fake_response("")
        )
        mock_client_cls.return_value = mock_client

        with pytest.raises(MediaSummaryError, match="empty youtube summary"):
            await summarizer.summarize_youtube("https://www.youtube.com/watch?v=abc123")


async def test_youtube_custom_max_seconds() -> None:
    summarizer = GeminiVideoSummarizer(api_key="test-key", youtube_max_seconds=30)
    captured_contents: list[Any] = []

    with patch("google.genai.Client") as mock_client_cls:
        mock_client = MagicMock()

        async def _capture_generate(*args: Any, **kwargs: Any) -> Any:
            captured_contents.append(kwargs.get("contents"))
            return _fake_response("description")

        mock_client.aio.models.generate_content = _capture_generate
        mock_client_cls.return_value = mock_client

        await summarizer.summarize_youtube("https://youtu.be/abc123")

    content = captured_contents[0]
    video_part = content.parts[0]
    assert video_part.video_metadata.end_offset == "30s"


async def test_youtube_video_metadata_unavailable_raises() -> None:
    """If the SDK rejects video_metadata, fail rather than process the whole video."""
    summarizer = GeminiVideoSummarizer(api_key="test-key")

    with patch("google.genai.Client") as mock_client_cls:
        mock_client = MagicMock()

        async def _raise_type_error(*args: Any, **kwargs: Any) -> Any:
            raise TypeError("unexpected keyword 'video_metadata'")

        mock_client.aio.models.generate_content = _raise_type_error
        mock_client_cls.return_value = mock_client

        with pytest.raises(MediaSummaryError, match="youtube video_metadata unavailable"):
            await summarizer.summarize_youtube("https://www.youtube.com/watch?v=abc123")


# --- SSRF guard tests -------------------------------------------------------


def _public_addrinfo(host: str, port: int | None = None) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """Fake getaddrinfo returning a public IP (8.8.8.8)."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port or 443))]


def _private_addrinfo(host: str, port: int | None = None) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """Fake getaddrinfo returning a private IP (10.0.0.1)."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port or 443))]


def _loopback_addrinfo(host: str, port: int | None = None) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """Fake getaddrinfo returning a loopback IP (127.0.0.1)."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 443))]


def _linklocal_addrinfo(host: str, port: int | None = None) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """Fake getaddrinfo returning a link-local IP (169.254.169.254)."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port or 443))]


async def test_ssrf_rejects_http_scheme() -> None:
    with pytest.raises(MediaSummaryError, match="non-https"):
        GeminiVideoSummarizer._assert_safe_url("http://example.com/video.mp4")


async def test_ssrf_rejects_localhost() -> None:
    with patch("socket.getaddrinfo", side_effect=_loopback_addrinfo), pytest.raises(MediaSummaryError, match="SSRF guard rejected"):
        GeminiVideoSummarizer._assert_safe_url("https://localhost/video.mp4")


async def test_ssrf_rejects_private_ip() -> None:
    with patch("socket.getaddrinfo", side_effect=_private_addrinfo), pytest.raises(MediaSummaryError, match="SSRF guard rejected"):
        GeminiVideoSummarizer._assert_safe_url("https://internal.example.com/video.mp4")


async def test_ssrf_rejects_link_local_metadata() -> None:
    with patch("socket.getaddrinfo", side_effect=_linklocal_addrinfo), pytest.raises(MediaSummaryError, match="SSRF guard rejected"):
        GeminiVideoSummarizer._assert_safe_url("https://metadata.example.com/video.mp4")


async def test_ssrf_accepts_public_ip() -> None:
    with patch("socket.getaddrinfo", side_effect=_public_addrinfo):
        # Should not raise.
        GeminiVideoSummarizer._assert_safe_url("https://cdn.example.com/video.mp4")


async def test_ssrf_rejects_dns_failure() -> None:
    with patch("socket.getaddrinfo", side_effect=socket.gaierror("no such host")), pytest.raises(MediaSummaryError, match="DNS resolution failed"):
        GeminiVideoSummarizer._assert_safe_url("https://nonexistent.example.com/video.mp4")


async def test_ssrf_rejects_redirect_to_private_ip() -> None:
    """A redirect to a private IP must be rejected before connecting."""
    summarizer = GeminiVideoSummarizer(api_key="test-key")

    redirect_response = MagicMock()
    redirect_response.status_code = 302
    redirect_response.headers = {"location": "https://internal.evil.com/video.mp4"}

    async def _redirect_aread() -> None:
        pass

    redirect_response.aread = _redirect_aread

    def _addrinfo_by_host(host: str, port: int | None = None) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        # First hop (cdn.example.com) is public; redirect target (internal.evil.com) is private.
        if "internal" in host:
            return _private_addrinfo(host, port)
        return _public_addrinfo(host, port)

    with (
        patch.object(
            httpx.AsyncClient, "stream",
            return_value=_FakeStreamContext(redirect_response),
        ),
        patch("socket.getaddrinfo", side_effect=_addrinfo_by_host),
        pytest.raises(MediaSummaryError, match="SSRF guard rejected"),
    ):
        await summarizer.summarize_media(
            download_url="https://cdn.example.com/video.mp4",
            mime_type="video/mp4",
            file_name="video.mp4",
            message_type="video",
        )


async def test_ssrf_follows_safe_redirect() -> None:
    """A redirect to a public IP is followed and the video is downloaded."""
    summarizer = GeminiVideoSummarizer(api_key="test-key")
    video_bytes = b"\x00" * 50

    redirect_response = MagicMock()
    redirect_response.status_code = 302
    redirect_response.headers = {"location": "https://cdn2.example.com/video.mp4"}

    async def _redirect_aread() -> None:
        pass

    redirect_response.aread = _redirect_aread

    final_response = _make_stream_response(video_bytes)

    call_count = 0

    class _RedirectThenFinalContext:
        def __init__(self) -> None:
            pass

        async def __aenter__(self) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return redirect_response
            return final_response

        async def __aexit__(self, *args: object) -> None:
            pass

    with (
        patch.object(
            httpx.AsyncClient, "stream",
            return_value=_RedirectThenFinalContext(),
        ),
        patch("socket.getaddrinfo", side_effect=_public_addrinfo),
        patch("google.genai.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=_fake_response("description")
        )
        mock_client_cls.return_value = mock_client

        result = await summarizer.summarize_media(
            download_url="https://cdn.example.com/video.mp4",
            mime_type="video/mp4",
            file_name="video.mp4",
            message_type="video",
        )

    assert result.kind == "video"
    assert call_count == 2  # redirect + final


async def test_ssrf_rejects_url_with_no_hostname() -> None:
    with pytest.raises(MediaSummaryError, match="no hostname"):
        GeminiVideoSummarizer._assert_safe_url("https:///video.mp4")


async def test_ssrf_rejects_redirect_with_no_location() -> None:
    """A 3xx response with no Location header is rejected."""
    summarizer = GeminiVideoSummarizer(api_key="test-key")

    redirect_response = MagicMock()
    redirect_response.status_code = 302
    redirect_response.headers = {}  # no Location

    async def _redirect_aread() -> None:
        pass

    redirect_response.aread = _redirect_aread

    with (
        patch.object(
            httpx.AsyncClient, "stream",
            return_value=_FakeStreamContext(redirect_response),
        ),
        patch("socket.getaddrinfo", side_effect=_public_addrinfo),
        pytest.raises(MediaSummaryError, match="redirect with no Location"),
    ):
        await summarizer.summarize_media(
            download_url="https://cdn.example.com/video.mp4",
            mime_type="video/mp4",
            file_name="video.mp4",
            message_type="video",
        )


async def test_ssrf_rejects_too_many_redirects() -> None:
    """More than _MAX_REDIRECTS hops is rejected."""
    from echo_v2.services.gemini_video_summarizer import _MAX_REDIRECTS

    summarizer = GeminiVideoSummarizer(api_key="test-key")

    redirect_response = MagicMock()
    redirect_response.status_code = 302
    redirect_response.headers = {"location": "https://cdn.example.com/video.mp4"}

    async def _redirect_aread() -> None:
        pass

    redirect_response.aread = _redirect_aread

    stream_call_count = 0

    class _AlwaysRedirectContext:
        async def __aenter__(self) -> Any:
            return redirect_response

        async def __aexit__(self, *args: object) -> None:
            pass

    def _counting_stream(*args: Any, **kwargs: Any) -> Any:
        nonlocal stream_call_count
        stream_call_count += 1
        return _AlwaysRedirectContext()

    with (
        patch.object(httpx.AsyncClient, "stream", side_effect=_counting_stream),
        patch("socket.getaddrinfo", side_effect=_public_addrinfo),
        pytest.raises(MediaSummaryError, match="too many redirects"),
    ):
        await summarizer.summarize_media(
            download_url="https://cdn.example.com/video.mp4",
            mime_type="video/mp4",
            file_name="video.mp4",
            message_type="video",
        )

    # Should have attempted _MAX_REDIRECTS + 1 hops before giving up.
    assert stream_call_count == _MAX_REDIRECTS + 1
