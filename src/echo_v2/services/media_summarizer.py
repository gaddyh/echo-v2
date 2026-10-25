"""Provider-neutral media summarization port and OpenAI implementation.

Mirrors the transcription pattern (:mod:`echo_v2.services.transcription`):
a ``MediaSummarizer`` protocol defines the boundary between the
application and any summarization provider, and ``OpenAIMediaSummarizer``
implements it using the injected OpenAI client.

Three media kinds are supported, all producing a short text summary
suitable for inclusion in the conversation transcript passed to the
WaitingForMe analyzer:

* **Image** — sent to a vision-capable model (``gpt-4o``) as a base64
  image URL input. The caption (if any) is included as context.
* **Document** — downloaded; if it is a text-like file (``text/*``,
  ``application/json``, ``application/pdf`` handled by the vision model,
  or a plain-text extension) the bytes are decoded and summarized.
  Binary/unknown document types fall back to a generic
  "[document: <file_name>]" placeholder so the analyzer still sees that
  a document was shared.
* **Link** — a URL appearing in a text message (or a text-only message
  that is itself a URL) is fetched and the page text is summarized.
  Link summarization is intentionally best-effort: fetch failures and
  non-HTML content degrade gracefully to a placeholder.

The summaries are deliberately short (one or two sentences) and in the
same language as the source content, matching the "basic summary" scope.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from echo_v2.domain.ai_usage_event import AiUsageStatus, AiUsageType

if TYPE_CHECKING:
    from echo_v2.services.reporting.ai_usage_recorder import AiUsageRecorder

__all__ = [
    "MediaSummarizer",
    "MediaSummary",
    "MediaSummaryError",
    "OpenAIMediaSummarizer",
    "classify_video_url",
    "download_media",
    "extract_urls",
    "is_link_message",
    "is_video_link_message",
    "summarize_document_bytes",
    "summarize_image_bytes",
    "summarize_link",
]

_logger = logging.getLogger("echo_v2.services.media_summarizer")

# Text-like MIME types we can decode and feed directly to a text model.
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/javascript",
}
_TEXT_FILE_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".xml", ".yaml", ".yml",
    ".log", ".tsv", ".rtf", ".html", ".htm", ".js", ".ts", ".py", ".sql",
}

# Vision-capable model used for image and PDF summarization.
_VISION_MODEL = "gpt-4o"
# Text model used for document text and link summarization.
_TEXT_MODEL = "gpt-4.1"

_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MB safety cap for media downloads.
_MAX_LINK_BYTES = 500_000  # 500 KB cap for fetched link HTML/text.
_MAX_DOC_TEXT_CHARS = 12_000  # Truncate document text before sending to the LLM.
_MAX_LINK_TEXT_CHARS = 12_000  # Truncate fetched link text before summarizing.

# Regex to detect URLs in text messages. Matches http(s):// URLs that are
# not surrounded by whitespace-only context (i.e. the message is "mostly a URL").
# We only summarize links when the text is a URL or is dominated by a URL,
# to avoid summarizing every casual mention of a link in a conversation.
_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)


def extract_urls(text: str) -> list[str]:
    """Extract HTTP(S) URLs from ``text``.

    Returns a de-duplicated list preserving order of first appearance.
    Trailing punctuation (``.``, ``,``, ``)``, ``]``) is stripped from
    each URL.
    """
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,)];:")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def is_link_message(text: str | None) -> bool:
    """Heuristic: is this text message "mostly a link"?

    Returns True if the text, after stripping, is a single URL or is
    very short text wrapping a URL (e.g. "check this out https://...").
    We avoid summarizing links buried in long conversational text.
    """
    if not text or not text.strip():
        return False
    stripped = text.strip()
    urls = extract_urls(stripped)
    if not urls:
        return False
    # If the text is just a URL (possibly with trailing whitespace).
    if stripped in urls or all(stripped == u or stripped.endswith(u) for u in urls):
        return True
    # If the text is short and contains a URL (e.g. "look: <url>").
    non_url_text = stripped
    for url in urls:
        non_url_text = non_url_text.replace(url, "")
    non_url_text = non_url_text.strip(" :.-")
    return len(non_url_text) <= 60 and len(urls) == 1


# Video file extensions recognized in direct video URLs.
_VIDEO_FILE_EXTS = {".mp4", ".mov", ".webm", ".mkv"}

# YouTube hosts (lowercase, without scheme).
_YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


def classify_video_url(url: str) -> str | None:
    """Classify a URL as a direct video file or a YouTube watch URL.

    Returns ``"direct_video"`` if the URL path ends with a recognized
    video extension (``.mp4``/``.mov``/``.webm``/``.mkv``), ``"youtube"``
    if the URL points at a recognized YouTube watch/shorts/embed path, or
    ``None`` if it is neither.
    """
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return None
    host = (parsed.netloc or "").lower().removeprefix("www.")
    path = parsed.path.lower()
    # Direct video file: path ends with a recognized extension.
    ext = Path(path).suffix
    if ext in _VIDEO_FILE_EXTS:
        return "direct_video"
    # YouTube watch / shorts / embed / youtu.be short link.
    if host in _YOUTUBE_HOSTS:
        if host == "youtu.be":
            # youtu.be/<id> — non-empty path beyond the leading slash.
            if len(path.strip("/")) > 0:
                return "youtube"
            return None
        if path == "/watch" or path.startswith(("/shorts/", "/embed/")):
            return "youtube"
    return None


def is_video_link_message(text: str | None) -> bool:
    """Heuristic: is this text message "mostly a video link"?

    Combines :func:`is_link_message` (the "mostly a link" gate) with
    :func:`classify_video_url` so casual YouTube/video mentions buried in
    long conversational text are not analyzed.
    """
    if not is_link_message(text):
        return False
    urls = extract_urls(text or "")
    if not urls:
        return False
    return classify_video_url(urls[0]) is not None


@dataclass(frozen=True)
class MediaSummary:
    """Result of summarizing a media item.

    ``text`` is the short summary to use in place of (or alongside) the
    raw media in the conversation transcript. ``kind`` records which
    summarization path was taken, for logging/tracing only.
    """

    text: str
    kind: str = "unknown"
    model: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict)


class MediaSummaryError(RuntimeError):
    """Provider-neutral media summarization failure."""


@runtime_checkable
class MediaSummarizer(Protocol):
    """Summarize a media message into a short text description.

    The summarizer is given the ``media_download_url``, ``media_mime_type``,
    ``media_file_name``, and ``message_type`` (``image``/``video``/
    ``document``) for downloaded media, or a ``link_url`` for link
    summarization. It returns a :class:`MediaSummary` whose ``text`` is a
    short description suitable for the conversation transcript.
    """

    async def summarize_media(
        self,
        *,
        download_url: str,
        mime_type: str | None,
        file_name: str | None,
        message_type: str,
        caption: str | None = None,
        child_user_id: str | None = None,
    ) -> MediaSummary: ...

    async def summarize_link(
        self, url: str, *, child_user_id: str | None = None
    ) -> MediaSummary: ...

    async def summarize_youtube(self, url: str) -> MediaSummary: ...

    async def close(self) -> None: ...


class OpenAIMediaSummarizer:
    """Implements :class:`MediaSummarizer` using the injected OpenAI client.

    The client is injected (not created per-call) so it can be wrapped
    with ``langsmith.wrappers.wrap_openai`` for tracing, matching the
    analyzer pattern.

    Args:
        client: An OpenAI-compatible async client (e.g.
            ``wrap_openai(AsyncOpenAI(...))``).
        vision_model: Model for image/PDF vision input. Default ``gpt-4o``.
        text_model: Model for document text and link summarization.
            Default ``gpt-4.1``.
        http_client: Optional pre-configured ``httpx.AsyncClient`` for
            media/link downloads. If ``None``, a short-lived client is
            created per download.
    """

    def __init__(
        self,
        client: Any,
        *,
        vision_model: str = _VISION_MODEL,
        text_model: str = _TEXT_MODEL,
        http_client: httpx.AsyncClient | None = None,
        video_summarizer: MediaSummarizer | None = None,
        recorder: AiUsageRecorder | None = None,
    ) -> None:
        self._client = client
        self._vision_model = vision_model
        self._text_model = text_model
        self._http_client = http_client
        self._video_summarizer = video_summarizer
        self._recorder = recorder

    async def summarize_media(
        self,
        *,
        download_url: str,
        mime_type: str | None,
        file_name: str | None,
        message_type: str,
        caption: str | None = None,
        child_user_id: str | None = None,
    ) -> MediaSummary:
        if message_type == "image":
            return await self._summarize_image(
                download_url=download_url,
                mime_type=mime_type or "image/jpeg",
                caption=caption,
                child_user_id=child_user_id,
            )
        if message_type == "video":
            if self._video_summarizer is not None:
                return await self._video_summarizer.summarize_media(
                    download_url=download_url,
                    mime_type=mime_type,
                    file_name=file_name,
                    message_type=message_type,
                    caption=caption,
                )
            # No Gemini video summarizer configured — fall back to placeholder.
            return MediaSummary(
                text=_video_placeholder(caption, file_name),
                kind="video_placeholder",
            )
        if message_type == "document":
            return await self._summarize_document(
                download_url=download_url,
                mime_type=mime_type,
                file_name=file_name,
                caption=caption,
                child_user_id=child_user_id,
            )
        return MediaSummary(
            text=f"[unsupported media: {message_type}]",
            kind="unsupported",
        )

    async def summarize_link(
        self, url: str, *, child_user_id: str | None = None
    ) -> MediaSummary:
        try:
            return await summarize_link(
                client=self._client,
                model=self._text_model,
                url=url,
                http_client=self._http_client,
                recorder=self._recorder,
                child_user_id=child_user_id,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort link summary
            _logger.warning("link summarization failed for %s: %s", url, exc)
            return MediaSummary(text=f"[link: {url}]", kind="link_error")

    async def summarize_youtube(self, url: str) -> MediaSummary:
        if self._video_summarizer is None:
            raise RuntimeError("video summarizer is not configured")
        return await self._video_summarizer.summarize_youtube(url)

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()

    async def _summarize_image(
        self,
        *,
        download_url: str,
        mime_type: str,
        caption: str | None,
        child_user_id: str | None = None,
    ) -> MediaSummary:
        try:
            media_bytes = await download_media(download_url, self._http_client)
        except Exception as exc:  # noqa: BLE001 - best-effort
            _logger.warning("image download failed for %s: %s", download_url, exc)
            return MediaSummary(text=_image_placeholder(caption), kind="image_download_error")
        return await summarize_image_bytes(
            client=self._client,
            model=self._vision_model,
            image_bytes=media_bytes,
            mime_type=mime_type,
            caption=caption,
            recorder=self._recorder,
            child_user_id=child_user_id,
        )

    async def _summarize_document(
        self,
        *,
        download_url: str,
        mime_type: str | None,
        file_name: str | None,
        caption: str | None,
        child_user_id: str | None = None,
    ) -> MediaSummary:
        try:
            media_bytes = await download_media(download_url, self._http_client)
        except Exception as exc:  # noqa: BLE001 - best-effort
            _logger.warning("document download failed for %s: %s", download_url, exc)
            return MediaSummary(
                text=_document_placeholder(caption, file_name),
                kind="document_download_error",
            )
        return await summarize_document_bytes(
            client=self._client,
            vision_model=self._vision_model,
            text_model=self._text_model,
            document_bytes=media_bytes,
            mime_type=mime_type,
            file_name=file_name,
            caption=caption,
            recorder=self._recorder,
            child_user_id=child_user_id,
        )


# --- Download helper -------------------------------------------------------


async def download_media(
    url: str,
    http_client: httpx.AsyncClient | None = None,
) -> bytes:
    """Download a media file from a direct URL, capped at ``_MAX_DOWNLOAD_BYTES``."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MediaSummaryError("media download URL must be an absolute HTTP(S) URL")
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=120, follow_redirects=True)
    try:
        response = await client.get(url)
        response.raise_for_status()
        content = response.content
        if len(content) > _MAX_DOWNLOAD_BYTES:
            raise MediaSummaryError(
                f"media download exceeded {_MAX_DOWNLOAD_BYTES} bytes: {len(content)}"
            )
        return content
    finally:
        if owns_client:
            await client.aclose()


# --- Image summarization ---------------------------------------------------


async def summarize_image_bytes(
    *,
    client: Any,
    model: str,
    image_bytes: bytes,
    mime_type: str,
    caption: str | None = None,
    recorder: AiUsageRecorder | None = None,
    child_user_id: str | None = None,
) -> MediaSummary:
    """Summarize an image via a vision-capable model.

    The image is sent as a base64 data URL in the user message. The model
    is asked for a one-or-two-sentence description in the source language.
    """
    normalized_mime = mime_type.split(";")[0].strip().lower() or "image/jpeg"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{normalized_mime};base64,{b64}"

    prompt = (
        "Describe this image in one or two short sentences. "
        "If there is text in the image, mention what it says. "
        "Reply in the language of any text visible in the image, "
        "otherwise reply in English."
    )
    user_content: list[dict[str, Any]] = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    if caption:
        user_content.insert(
            0,
            {"type": "text", "text": f"Caption from sender: {caption}"},
        )

    invocation_id = uuid4()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": user_content}],
            temperature=0,
            max_completion_tokens=200,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort
        _logger.warning("image vision summary failed: %s", exc)
        await _record_media_usage(
            recorder,
            operation="media_image",
            model=model,
            child_user_id=child_user_id,
            status=AiUsageStatus.ERROR,
            invocation_id=invocation_id,
            error_type=type(exc).__name__,
        )
        return MediaSummary(text=_image_placeholder(caption), kind="image_vision_error")

    usage = getattr(response, "usage", None)
    await _record_media_usage(
        recorder,
        operation="media_image",
        model=model,
        child_user_id=child_user_id,
        status=AiUsageStatus.SUCCESS,
        invocation_id=invocation_id,
        usage=usage,
    )
    text = (response.choices[0].message.content or "").strip()
    if not text:
        return MediaSummary(text=_image_placeholder(caption), kind="image_empty")
    return MediaSummary(text=text, kind="image", model=model)


# --- Document summarization -----------------------------------------------


async def summarize_document_bytes(
    *,
    client: Any,
    vision_model: str,
    text_model: str,
    document_bytes: bytes,
    mime_type: str | None,
    file_name: str | None,
    caption: str | None = None,
    recorder: AiUsageRecorder | None = None,
    child_user_id: str | None = None,
) -> MediaSummary:
    """Summarize a document.

    Text-like documents are decoded and summarized by the text model.
    PDFs are sent to the vision model as a base64 file (gpt-4o accepts
    PDF input). Other binary types fall back to a placeholder so the
    analyzer still sees that a document was shared.
    """
    normalized_mime = (mime_type or "").split(";")[0].strip().lower()
    ext = Path(file_name or "").suffix.lower()

    if _is_text_document(normalized_mime, ext):
        try:
            text = document_bytes.decode("utf-8", errors="replace")
        except UnicodeDecodeError:  # pragma: no cover - defensive, utf-8 replace never raises
            text = document_bytes.decode("latin-1", errors="replace")
        text = text[:_MAX_DOC_TEXT_CHARS]
        return await _summarize_text(
            client=client,
            model=text_model,
            text=text,
            label="document",
            caption=caption,
            file_name=file_name,
            recorder=recorder,
            child_user_id=child_user_id,
            operation="media_document",
        )

    if normalized_mime == "application/pdf":
        return await _summarize_pdf(
            client=client,
            model=vision_model,
            pdf_bytes=document_bytes,
            caption=caption,
            file_name=file_name,
            recorder=recorder,
            child_user_id=child_user_id,
        )

    return MediaSummary(
        text=_document_placeholder(caption, file_name),
        kind="document_unsupported",
    )


def _is_text_document(mime_type: str, ext: str) -> bool:
    if any(mime_type.startswith(prefix) for prefix in _TEXT_MIME_PREFIXES):
        return True
    if mime_type in _TEXT_MIME_TYPES:
        return True
    return ext in _TEXT_FILE_EXTS

async def _summarize_text(
    *,
    client: Any,
    model: str,
    text: str,
    label: str,
    caption: str | None,
    file_name: str | None,
    recorder: AiUsageRecorder | None = None,
    child_user_id: str | None = None,
    operation: str = "media_document",
) -> MediaSummary:
    name_hint = f" (file: {file_name})" if file_name else ""
    cap_hint = f" Sender caption: {caption}" if caption else ""
    prompt = (
        f"Summarize the following {label} content in one or two short sentences. "
        f"Reply in the language of the content.{name_hint}{cap_hint}\n\n"
        f"{text}"
    )
    invocation_id = uuid4()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_completion_tokens=200,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort
        _logger.warning("%s text summary failed: %s", label, exc)
        await _record_media_usage(
            recorder,
            operation=operation,
            model=model,
            child_user_id=child_user_id,
            status=AiUsageStatus.ERROR,
            invocation_id=invocation_id,
            error_type=type(exc).__name__,
        )
        return MediaSummary(
            text=_document_placeholder(caption, file_name),
            kind=f"{label}_summary_error",
        )
    usage = getattr(response, "usage", None)
    await _record_media_usage(
        recorder,
        operation=operation,
        model=model,
        child_user_id=child_user_id,
        status=AiUsageStatus.SUCCESS,
        invocation_id=invocation_id,
        usage=usage,
    )
    summary = (response.choices[0].message.content or "").strip()
    if not summary:
        return MediaSummary(text=_document_placeholder(caption, file_name), kind=f"{label}_empty")
    return MediaSummary(text=summary, kind=label, model=model)


async def _summarize_pdf(
    *,
    client: Any,
    model: str,
    pdf_bytes: bytes,
    caption: str | None,
    file_name: str | None,
    recorder: AiUsageRecorder | None = None,
    child_user_id: str | None = None,
) -> MediaSummary:
    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    prompt = (
        "Summarize this document in one or two short sentences. "
        "Reply in the language of the document."
    )
    user_content: list[dict[str, Any]] = [
        {"type": "text", "text": prompt},
        {
            "type": "file",
            "file": {
                "filename": file_name or "document.pdf",
                "file_data": f"data:application/pdf;base64,{b64}",
            },
        },
    ]
    if caption:
        user_content.insert(
            0,
            {"type": "text", "text": f"Sender caption: {caption}"},
        )
    invocation_id = uuid4()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": user_content}],
            temperature=0,
            max_completion_tokens=200,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort
        _logger.warning("pdf vision summary failed: %s", exc)
        await _record_media_usage(
            recorder,
            operation="media_document",
            model=model,
            child_user_id=child_user_id,
            status=AiUsageStatus.ERROR,
            invocation_id=invocation_id,
            error_type=type(exc).__name__,
        )
        return MediaSummary(text=_document_placeholder(caption, file_name), kind="pdf_error")
    usage = getattr(response, "usage", None)
    await _record_media_usage(
        recorder,
        operation="media_document",
        model=model,
        child_user_id=child_user_id,
        status=AiUsageStatus.SUCCESS,
        invocation_id=invocation_id,
        usage=usage,
    )
    text = (response.choices[0].message.content or "").strip()
    if not text:
        return MediaSummary(text=_document_placeholder(caption, file_name), kind="pdf_empty")
    return MediaSummary(text=text, kind="document", model=model)


# --- Link summarization ----------------------------------------------------


async def summarize_link(
    *,
    client: Any,
    model: str,
    url: str,
    http_client: httpx.AsyncClient | None = None,
    recorder: AiUsageRecorder | None = None,
    child_user_id: str | None = None,
) -> MediaSummary:
    """Fetch a URL and summarize its text content.

    Best-effort: any fetch or parse failure raises ``MediaSummaryError``,
    which the caller is expected to catch and degrade to a placeholder.
    """
    owns_client = http_client is None
    hc = http_client or httpx.AsyncClient(timeout=30, follow_redirects=True)
    try:
        response = await hc.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        body = response.text[:_MAX_LINK_BYTES]
    except Exception as exc:
        raise MediaSummaryError(f"link fetch failed: {exc}") from exc
    finally:
        if owns_client:
            await hc.aclose()

    if "html" in content_type:
        text = _strip_html(body)
    else:
        text = body
    text = text[:_MAX_LINK_TEXT_CHARS]
    if not text.strip():
        # JS-rendered pages (e.g. Google Search) may have empty body
        # text. Try extracting useful info from the final redirect URL.
        # The fallback is already a concise description — return it
        # directly instead of sending it through the LLM, which would
        # hedge ("Sorry, I can't access...") since it's metadata, not
        # page content.
        fallback = _extract_url_fallback(response.url)
        if fallback:
            return MediaSummary(text=fallback, kind="link_url_fallback")
        return MediaSummary(text=f"[link: {url}]", kind="link_empty")

    return await _summarize_text(
        client=client,
        model=model,
        text=text,
        label="link",
        caption=None,
        file_name=url,
        recorder=recorder,
        child_user_id=child_user_id,
        operation="media_link",
    )


async def _record_media_usage(
    recorder: AiUsageRecorder | None,
    *,
    operation: str,
    model: str,
    child_user_id: str | None,
    status: str,
    invocation_id: Any,
    usage: Any = None,
    error_type: str | None = None,
) -> None:
    """Record one billable OpenAI media provider attempt. Best-effort."""
    if recorder is None:
        return
    from echo_v2.services.reporting.ai_usage_recorder import UsageContext, UsagePayload

    def _uv(field: str) -> int | None:
        value = getattr(usage, field, None) if usage is not None else None
        return value if isinstance(value, int) else None

    context = UsageContext(
        operation=operation,
        provider="openai",
        model=model,
        child_user_id=child_user_id,
    )
    payload = UsagePayload(
        usage_type=AiUsageType.TOKENS,
        status=status,
        prompt_tokens=_uv("prompt_tokens"),
        completion_tokens=_uv("completion_tokens"),
        total_tokens=_uv("total_tokens"),
        invocation_id=invocation_id,
        error_type=error_type if status == AiUsageStatus.ERROR else None,
    )
    await recorder.record(context, payload)


def _extract_url_fallback(final_url: str | object) -> str | None:
    """Extract useful text from a redirect destination URL.

    When the page body is empty (JS-rendered pages like Google Search),
    the final URL after redirects may still contain useful information
    — e.g. a Google Search URL has the search query in the ``q``
    parameter. This handles ``share.google`` short links, which
    redirect to Google Search pages whose raw HTML has no extractable
    text.

    Returns a short descriptive string, or ``None`` if nothing useful
    can be extracted.
    """
    from urllib.parse import parse_qs, unquote, urlparse

    try:
        parsed = urlparse(str(final_url))
    except Exception:  # noqa: BLE001 - defensive, best-effort
        return None

    host = parsed.netloc.lower()
    path = parsed.path.lower()
    qs = parse_qs(parsed.query)

    # Google Search pages (share.google short links redirect here).
    if "google.com" in host and "/search" in path:
        q = qs.get("q", [None])[0]
        if q:
            ibp = qs.get("ibp", [None])[0]
            suffix = " (video)" if ibp == "video" else ""
            return f"Google search for: {unquote(q)}{suffix}"
        return None

    # YouTube watch URLs.
    if ("youtube.com" in host and "/watch" in path) or "youtu.be" in host:
        v = qs.get("v", [None])[0]
        if not v and "youtu.be" in host:
            v = parsed.path.strip("/")
        if v:
            return f"YouTube video: {v}"
        return None

    return None


def _strip_html(html: str) -> str:
    """Crude HTML-to-text: strip tags and collapse whitespace.

    Good enough for a basic summary without pulling in a parser
    dependency. Extracts ``<title>`` and visible text.
    """
    import re

    # Extract title for context.
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else ""

    # Remove script/style blocks.
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.IGNORECASE | re.DOTALL)
    # Remove all tags.
    text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    if title and title not in text[: len(title) + 50]:
        text = f"{title}. {text}"
    return text


# --- Placeholders ----------------------------------------------------------


def _image_placeholder(caption: str | None) -> str:
    if caption:
        return f"[image: {caption}]"
    return "[image]"


def _document_placeholder(caption: str | None, file_name: str | None) -> str:
    parts = []
    if file_name:
        parts.append(file_name)
    if caption:
        parts.append(caption)
    if parts:
        return f"[document: {', '.join(parts)}]"
    return "[document]"


def _video_placeholder(caption: str | None, file_name: str | None) -> str:
    parts = []
    if file_name:
        parts.append(file_name)
    if caption:
        parts.append(caption)
    if parts:
        return f"[video: {', '.join(parts)}]"
    return "[video]"
