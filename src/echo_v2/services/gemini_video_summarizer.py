"""Gemini-based video summarization adapter.

Implements the :class:`echo_v2.services.media_summarizer.MediaSummarizer`
protocol for the ``video`` message kind. The whole video file is sent
inline to Gemini (no local trimming); a configurable size cap rejects
oversized files without buffering them fully into memory.

Gemini describes the video; the existing Guard analyzers decide whether
to alert. No risk classification or notification policy is added here.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from echo_v2.services.media_summarizer import MediaSummary, MediaSummaryError

__all__ = [
    "DEFAULT_GEMINI_VIDEO_MAX_BYTES",
    "DEFAULT_GEMINI_VIDEO_MAX_SECONDS",
    "DEFAULT_GEMINI_VIDEO_MODEL",
    "DEFAULT_GEMINI_VIDEO_PROMPT",
    "GeminiVideoSummarizer",
    "VideoTooLargeError",
]

_logger = logging.getLogger("echo_v2.services.gemini_video_summarizer")

DEFAULT_GEMINI_VIDEO_MODEL = "gemini-3.5-flash-lite"
DEFAULT_GEMINI_VIDEO_MAX_BYTES = 15 * 1024 * 1024  # 15 MB conservative pilot cap.
DEFAULT_GEMINI_VIDEO_MAX_SECONDS = 60  # Cap YouTube analysis to first 60 seconds.

# Maximum HTTP redirect hops followed by the SSRF-safe download.
_MAX_REDIRECTS = 5

DEFAULT_GEMINI_VIDEO_PROMPT = (
    "Describe what happens in this video, based on visual and audio evidence.\n"
    "Focus on:\n"
    "- People's actions and interactions\n"
    "- Spoken words and tone\n"
    "- Visible text\n"
    "- Signs of aggression, humiliation, threats, pressure, or distress\n"
    "- Important timestamps\n"
    "Be factual and concise (2–4 sentences).\n"
    "Do not infer intent or relationships without evidence.\n"
    "If content is unclear, say so.\n"
    "Do not decide whether to alert a parent.\n"
    "Return the description in Hebrew."
)


class VideoTooLargeError(MediaSummaryError):
    """Raised when a downloaded video exceeds the configured size cap.

    This is a deterministic failure: the enrichment layer should mark it
    terminal immediately rather than retrying.
    """


class GeminiVideoSummarizer:
    """Summarize a video using Gemini's native video understanding.

    Implements :class:`MediaSummarizer` for the ``video`` path only.
    Other ``message_type`` values return an ``unsupported`` summary and
    are never routed here in production.

    The whole video file is downloaded with a bounded stream and sent
    inline to Gemini. If the running byte total exceeds ``max_bytes`` the
    download is aborted and :class:`VideoTooLargeError` is raised so the
    enrichment layer can mark the attempt terminal.

    Args:
        api_key: Gemini API key.
        model: Gemini model id. Default ``gemini-3.8-flash``.
        max_bytes: Maximum accepted video size in bytes. Default 15 MB.
        http_client: Optional pre-configured ``httpx.AsyncClient`` for
            media downloads. If ``None``, a short-lived client is created
            per download.
        prompt: Optional prompt override (for tests / evaluation).
        youtube_max_seconds: Cap YouTube analysis to the first N seconds
            via Gemini's static processing config. Default 60.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_GEMINI_VIDEO_MODEL,
        max_bytes: int = DEFAULT_GEMINI_VIDEO_MAX_BYTES,
        http_client: httpx.AsyncClient | None = None,
        prompt: str | None = None,
        youtube_max_seconds: int = DEFAULT_GEMINI_VIDEO_MAX_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_bytes = max_bytes
        self._http_client = http_client
        self._prompt = prompt or DEFAULT_GEMINI_VIDEO_PROMPT
        self._youtube_max_seconds = youtube_max_seconds

    async def summarize_media(
        self,
        *,
        download_url: str,
        mime_type: str | None,
        file_name: str | None,
        message_type: str,
        caption: str | None = None,
    ) -> MediaSummary:
        if message_type != "video":
            return MediaSummary(
                text=f"[unsupported media: {message_type}]",
                kind="unsupported",
            )
        _logger.info(
            "gemini video summarizer start url=%s mime=%s file=%s caption=%s",
            download_url, mime_type, file_name, bool(caption),
        )
        video_bytes = await self._download_bounded(download_url)
        _logger.info(
            "gemini video summarizer downloaded bytes=%d (cap=%d)",
            len(video_bytes), self._max_bytes,
        )
        text = await self._analyze(video_bytes, mime_type or "video/mp4", caption)
        if not text or not text.strip():
            raise MediaSummaryError("gemini returned empty video summary")
        _logger.info(
            "gemini video summarizer complete chars=%d model=%s",
            len(text), self._model,
        )
        return MediaSummary(text=text, kind="video", model=self._model)

    async def summarize_youtube(self, url: str) -> MediaSummary:
        """Summarize a YouTube video by passing its URL directly to Gemini.

        Uses Gemini's Interactions API with a ``video`` uri and the
        existing prompt. A static processing config caps analysis to the
        first ``youtube_max_seconds`` seconds so a long video cannot
        become an unbounded request.

        If the installed ``google-genai`` SDK does not support the
        ``processing`` option, raises :class:`MediaSummaryError` rather
        than silently processing the entire video.
        """
        _logger.info("gemini youtube summarizer start url=%s max_seconds=%d", url, self._youtube_max_seconds)
        text = await self._analyze_youtube(url)
        if not text or not text.strip():
            raise MediaSummaryError("gemini returned empty youtube summary")
        _logger.info("gemini youtube summarizer complete chars=%d model=%s", len(text), self._model)
        return MediaSummary(text=text, kind="video", model=self._model)

    async def summarize_link(self, url: str) -> MediaSummary:
        raise NotImplementedError("GeminiVideoSummarizer does not support link summarization")

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()

    # --- internals -------------------------------------------------------

    @staticmethod
    def _assert_safe_url(url: str) -> None:
        """Validate that ``url`` is safe to fetch (SSRF guard).

        Requires HTTPS scheme. Resolves the hostname and rejects if any
        resolved address is private, loopback, link-local, reserved, or
        unspecified. For HTTPS, TLS certificate verification additionally
        prevents DNS rebinding between this validation and the actual
        connection — an attacker cannot obtain a valid cert for the
        original hostname pointing at an internal IP.
        """
        try:
            parsed = urlparse(url)
        except (ValueError, TypeError) as exc:
            raise MediaSummaryError(f"invalid URL: {exc}") from exc
        if parsed.scheme != "https":
            raise MediaSummaryError(f"non-https URL rejected (SSRF guard): {parsed.scheme}")
        hostname = parsed.hostname
        if not hostname:
            raise MediaSummaryError("URL has no hostname")
        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror as exc:
            raise MediaSummaryError(f"DNS resolution failed for {hostname}: {exc}") from exc
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_unspecified
                or str(ip) == "169.254.169.254"
            ):
                raise MediaSummaryError(
                    f"SSRF guard rejected {hostname}: resolves to {ip}"
                )

    async def _download_bounded(self, url: str) -> bytes:
        """Download ``url`` with a streaming read, aborting on size overflow.

        SSRF-safe: validates every destination (including each redirect
        hop) **before** connecting. Auto-redirects are disabled; 3xx
        responses are followed manually with re-validation of each
        ``Location``.

        Accumulates chunks into a ``bytearray`` and raises
        :class:`VideoTooLargeError` as soon as the running total exceeds
        ``max_bytes``, without buffering the entire oversized file.
        """
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=120, follow_redirects=False)
        try:
            current_url = url
            for _hop in range(_MAX_REDIRECTS + 1):
                self._assert_safe_url(current_url)
                async with client.stream("GET", current_url) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise MediaSummaryError(f"redirect with no Location from {current_url}")
                        # Read and discard any body before following the redirect.
                        await response.aread()
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    buf = bytearray()
                    async for chunk in response.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > self._max_bytes:
                            raise VideoTooLargeError(
                                f"video exceeded size cap {self._max_bytes} bytes "
                                f"(at least {len(buf)} bytes)"
                            )
                    return bytes(buf)
            raise MediaSummaryError(f"too many redirects (>{_MAX_REDIRECTS})")
        finally:
            if owns_client:
                await client.aclose()

    async def _analyze(
        self,
        video_bytes: bytes,
        mime_type: str,
        caption: str | None,
    ) -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self._api_key)
        parts: list[Any] = [
            types.Part.from_bytes(data=video_bytes, mime_type=mime_type),
            self._prompt,
        ]
        if caption:
            parts.append(f"\n\nCaption context (user-supplied): {caption}")
        response = await client.aio.models.generate_content(
            model=self._model,
            contents=parts,
        )
        return response.text or ""

    async def _analyze_youtube(self, url: str) -> str:
        """Analyze a YouTube video URL via Gemini's YouTube URL preview.

        Passes the URL directly to Gemini using ``types.Part`` with
        ``file_data`` (no local download). A ``video_metadata`` clipping
        interval caps analysis to the first ``youtube_max_seconds``
        seconds. Static processing (1 FPS frame extraction) is the
        Gemini default; clipping is only supported in static mode.

        Raises :class:`MediaSummaryError` if the SDK rejects the
        ``video_metadata`` option, rather than silently processing the
        entire video without a cap.
        """
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self._api_key)
        try:
            response = await client.aio.models.generate_content(
                model=self._model,
                contents=types.Content(parts=[
                    types.Part(
                        file_data=types.FileData(file_uri=url),
                        video_metadata=types.VideoMetadata(
                            start_offset="0s",
                            end_offset=f"{self._youtube_max_seconds}s",
                        ),
                    ),
                    types.Part(text=self._prompt),
                ]),
            )
        except TypeError as exc:
            # SDK rejects video_metadata — fail rather than retry without the cap.
            raise MediaSummaryError(
                f"youtube video_metadata unavailable: {exc}"
            ) from exc
        return response.text or ""
