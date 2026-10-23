"""Gemini-based video summarization adapter.

Implements the :class:`echo_v2.services.media_summarizer.MediaSummarizer`
protocol for the ``video`` message kind. The whole video file is sent
inline to Gemini (no local trimming); a configurable size cap rejects
oversized files without buffering them fully into memory.

Gemini describes the video; the existing Guard analyzers decide whether
to alert. No risk classification or notification policy is added here.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from echo_v2.services.media_summarizer import MediaSummary, MediaSummaryError

__all__ = [
    "DEFAULT_GEMINI_VIDEO_MAX_BYTES",
    "DEFAULT_GEMINI_VIDEO_MODEL",
    "DEFAULT_GEMINI_VIDEO_PROMPT",
    "GeminiVideoSummarizer",
    "VideoTooLargeError",
]

_logger = logging.getLogger("echo_v2.services.gemini_video_summarizer")

DEFAULT_GEMINI_VIDEO_MODEL = "gemini-3.8-flash"
DEFAULT_GEMINI_VIDEO_MAX_BYTES = 15 * 1024 * 1024  # 15 MB conservative pilot cap.

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
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_GEMINI_VIDEO_MODEL,
        max_bytes: int = DEFAULT_GEMINI_VIDEO_MAX_BYTES,
        http_client: httpx.AsyncClient | None = None,
        prompt: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_bytes = max_bytes
        self._http_client = http_client
        self._prompt = prompt or DEFAULT_GEMINI_VIDEO_PROMPT

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

    async def summarize_link(self, url: str) -> MediaSummary:
        raise NotImplementedError("GeminiVideoSummarizer does not support link summarization")

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()

    # --- internals -------------------------------------------------------

    async def _download_bounded(self, url: str) -> bytes:
        """Download ``url`` with a streaming read, aborting on size overflow.

        Accumulates chunks into a ``bytearray`` and raises
        :class:`VideoTooLargeError` as soon as the running total exceeds
        ``max_bytes``, without buffering the entire oversized file.
        """
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=120, follow_redirects=True)
        try:
            async with client.stream("GET", url) as response:
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
