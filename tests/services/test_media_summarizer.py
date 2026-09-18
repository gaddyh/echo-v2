"""Tests for the OpenAI-based media summarizer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from echo_v2.services.media_summarizer import (
    MediaSummary,
    OpenAIMediaSummarizer,
    _is_text_document,
    _strip_html,
    download_media,
    extract_urls,
    is_link_message,
    summarize_document_bytes,
    summarize_image_bytes,
    summarize_link,
)

# --- _is_text_document ------------------------------------------------------


def test_extract_urls_finds_http_urls():
    text = "check out https://example.com and http://foo.bar/baz"
    urls = extract_urls(text)
    assert urls == ["https://example.com", "http://foo.bar/baz"]


def test_extract_urls_strips_trailing_punctuation():
    text = "see (https://example.com/page). and https://other.com,"
    urls = extract_urls(text)
    assert urls == ["https://example.com/page", "https://other.com"]


def test_extract_urls_deduplicates():
    text = "https://example.com https://example.com"
    urls = extract_urls(text)
    assert urls == ["https://example.com"]


def test_extract_urls_no_urls_returns_empty():
    assert extract_urls("just some text") == []


def test_is_link_message_pure_url():
    assert is_link_message("https://example.com/article") is True


def test_is_link_message_short_prefix_with_url():
    assert is_link_message("check this: https://example.com") is True


def test_is_link_message_long_text_with_url_is_false():
    """Long conversational text with a URL should not be summarized."""
    long_text = (
        "hey i was thinking about the project and whether we should "
        "look at https://example.com for reference but let me know"
    )
    assert is_link_message(long_text) is False


def test_is_link_message_no_url_is_false():
    assert is_link_message("just a regular text message") is False


def test_is_link_message_none_is_false():
    assert is_link_message(None) is False


def test_is_link_message_empty_is_false():
    assert is_link_message("") is False


# --- _is_text_document ------------------------------------------------------


def test_is_text_document_text_mime_prefix():
    assert _is_text_document("text/plain", ".bin") is True


def test_is_text_document_json_mime():
    assert _is_text_document("application/json", ".bin") is True


def test_is_text_document_text_extension():
    assert _is_text_document("", ".md") is True
    assert _is_text_document("", ".csv") is True


def test_is_text_document_binary_mime():
    assert _is_text_document("application/pdf", ".pdf") is False
    assert _is_text_document("application/octet-stream", ".bin") is False


# --- _strip_html ------------------------------------------------------------


def test_strip_html_extracts_title_and_text():
    html = "<html><head><title>My Page</title></head><body><p>Hello world</p></body></html>"
    text = _strip_html(html)
    assert "My Page" in text
    assert "Hello world" in text


def test_strip_html_removes_script_and_style():
    html = (
        "<html><head><title>T</title><style>.x{}</style></head>"
        "<body><script>var x=1;</script><p>visible</p></body></html>"
    )
    text = _strip_html(html)
    assert "var x" not in text
    assert ".x{}" not in text
    assert "visible" in text


def test_strip_html_collapses_whitespace():
    html = "<p>a\n\n  b\t\tc</p>"
    text = _strip_html(html)
    assert "a b c" in text


# --- download_media ---------------------------------------------------------


@patch("echo_v2.services.media_summarizer.httpx.AsyncClient")
async def test_download_media_returns_bytes(MockClient: MagicMock):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = b"image-bytes"
    mock_response.raise_for_status = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    MockClient.return_value = mock_client

    result = await download_media("http://example.com/img.jpeg")
    assert result == b"image-bytes"


@patch("echo_v2.services.media_summarizer.httpx.AsyncClient")
async def test_download_media_rejects_oversized(MockClient: MagicMock):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = b"x" * (21 * 1024 * 1024)
    mock_response.raise_for_status = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    MockClient.return_value = mock_client

    from echo_v2.services.media_summarizer import MediaSummaryError

    with pytest.raises(MediaSummaryError, match="exceeded"):
        await download_media("http://example.com/big.jpeg")


# --- summarize_image_bytes --------------------------------------------------


async def test_summarize_image_bytes_returns_summary():
    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content="a red cat sitting"))]

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=fake_response)

    result = await summarize_image_bytes(
        client=client,
        model="gpt-4o",
        image_bytes=b"fake-image",
        mime_type="image/jpeg",
        caption=None,
    )
    assert isinstance(result, MediaSummary)
    assert result.text == "a red cat sitting"
    assert result.kind == "image"
    assert result.model == "gpt-4o"


async def test_summarize_image_bytes_with_caption():
    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content="a red cat"))]

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=fake_response)

    result = await summarize_image_bytes(
        client=client,
        model="gpt-4o",
        image_bytes=b"fake-image",
        mime_type="image/jpeg",
        caption="my cat",
    )
    assert result.text == "a red cat"


async def test_summarize_image_bytes_api_failure_returns_placeholder():
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("api down"))

    result = await summarize_image_bytes(
        client=client,
        model="gpt-4o",
        image_bytes=b"fake-image",
        mime_type="image/jpeg",
        caption=None,
    )
    assert result.text == "[image]"
    assert result.kind == "image_vision_error"


async def test_summarize_image_bytes_empty_response_returns_placeholder():
    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content=""))]

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=fake_response)

    result = await summarize_image_bytes(
        client=client,
        model="gpt-4o",
        image_bytes=b"fake-image",
        mime_type="image/jpeg",
        caption=None,
    )
    assert result.text == "[image]"
    assert result.kind == "image_empty"


# --- summarize_document_bytes -----------------------------------------------


async def test_summarize_document_bytes_text_document():
    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content="a quarterly report"))]

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=fake_response)

    result = await summarize_document_bytes(
        client=client,
        vision_model="gpt-4o",
        text_model="gpt-4.1",
        document_bytes=b"Q1 revenue was $10M",
        mime_type="text/plain",
        file_name="report.txt",
        caption=None,
    )
    assert result.text == "a quarterly report"
    assert result.kind == "document"


async def test_summarize_document_bytes_unsupported_binary_returns_placeholder():
    client = MagicMock()
    result = await summarize_document_bytes(
        client=client,
        vision_model="gpt-4o",
        text_model="gpt-4.1",
        document_bytes=b"\x00\x01\x02",
        mime_type="application/octet-stream",
        file_name="archive.zip",
        caption=None,
    )
    assert "archive.zip" in result.text
    assert result.kind == "document_unsupported"
    # The text model was never called for an unsupported binary type.
    client.chat.completions.create.assert_not_called()


async def test_summarize_document_bytes_text_summary_failure_returns_placeholder():
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("api down"))

    result = await summarize_document_bytes(
        client=client,
        vision_model="gpt-4o",
        text_model="gpt-4.1",
        document_bytes=b"some text",
        mime_type="text/plain",
        file_name="report.txt",
        caption=None,
    )
    assert "report.txt" in result.text
    assert result.kind == "document_summary_error"


# --- summarize_link ---------------------------------------------------------


@patch("echo_v2.services.media_summarizer.httpx.AsyncClient")
async def test_summarize_link_returns_summary(MockClient: MagicMock):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.headers = {"content-type": "text/html; charset=utf-8"}
    mock_response.text = "<html><head><title>News</title></head><body><p>Big event today</p></body></html>"
    mock_response.raise_for_status = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    MockClient.return_value = mock_client

    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content="big event happened"))]

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=fake_response)

    result = await summarize_link(
        client=client, model="gpt-4.1", url="https://example.com/news"
    )
    assert result.text == "big event happened"
    assert result.kind == "link"


@patch("echo_v2.services.media_summarizer.httpx.AsyncClient")
async def test_summarize_link_fetch_failure_raises(MockClient: MagicMock):
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=RuntimeError("network down"))
    mock_client.aclose = AsyncMock()
    MockClient.return_value = mock_client

    from echo_v2.services.media_summarizer import MediaSummaryError

    client = MagicMock()
    with pytest.raises(MediaSummaryError, match="link fetch failed"):
        await summarize_link(client=client, model="gpt-4.1", url="https://example.com/x")


# --- OpenAIMediaSummarizer end-to-end ---------------------------------------


async def test_openai_media_summarizer_image_path():
    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content="a dog"))]

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=fake_response)

    summarizer = OpenAIMediaSummarizer(client=client)
    with patch(
        "echo_v2.services.media_summarizer.download_media",
        new_callable=AsyncMock,
        return_value=b"fake-image-bytes",
    ):
        result = await summarizer.summarize_media(
            download_url="https://example.com/dog.jpeg",
            mime_type="image/jpeg",
            file_name="dog.jpeg",
            message_type="image",
            caption=None,
        )
    assert result.text == "a dog"
    assert result.kind == "image"


async def test_openai_media_summarizer_video_returns_placeholder():
    client = MagicMock()
    summarizer = OpenAIMediaSummarizer(client=client)
    result = await summarizer.summarize_media(
        download_url="https://example.com/clip.mp4",
        mime_type="video/mp4",
        file_name="clip.mp4",
        message_type="video",
        caption="funny clip",
    )
    assert "clip.mp4" in result.text
    assert "funny clip" in result.text
    assert result.kind == "video_placeholder"
    # No download or LLM call for video (placeholder path).
    client.chat.completions.create.assert_not_called()


async def test_openai_media_summarizer_unsupported_type():
    client = MagicMock()
    summarizer = OpenAIMediaSummarizer(client=client)
    result = await summarizer.summarize_media(
        download_url="https://example.com/x",
        mime_type=None,
        file_name=None,
        message_type="sticker",
        caption=None,
    )
    assert "unsupported" in result.text
    assert result.kind == "unsupported"


async def test_openai_media_summarizer_link_failure_returns_placeholder():
    client = MagicMock()
    summarizer = OpenAIMediaSummarizer(client=client)
    with patch(
        "echo_v2.services.media_summarizer.summarize_link",
        new_callable=AsyncMock,
        side_effect=RuntimeError("fetch failed"),
    ):
        result = await summarizer.summarize_link("https://example.com/news")
    assert "https://example.com/news" in result.text
    assert result.kind == "link_error"


# --- PDF summarization -----------------------------------------------------


async def test_summarize_pdf_returns_summary():
    from echo_v2.services.media_summarizer import _summarize_pdf

    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content="a quarterly report"))]

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=fake_response)

    result = await _summarize_pdf(
        client=client,
        model="gpt-4o",
        pdf_bytes=b"%PDF-1.4 fake",
        caption=None,
        file_name="report.pdf",
    )
    assert result.text == "a quarterly report"
    assert result.kind == "document"
    assert result.model == "gpt-4o"


async def test_summarize_pdf_with_caption():
    from echo_v2.services.media_summarizer import _summarize_pdf

    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content="a report"))]

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=fake_response)

    result = await _summarize_pdf(
        client=client,
        model="gpt-4o",
        pdf_bytes=b"%PDF-1.4 fake",
        caption="the Q1 report",
        file_name="report.pdf",
    )
    assert result.text == "a report"


async def test_summarize_pdf_api_failure_returns_placeholder():
    from echo_v2.services.media_summarizer import _summarize_pdf

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("api down"))

    result = await _summarize_pdf(
        client=client,
        model="gpt-4o",
        pdf_bytes=b"%PDF-1.4 fake",
        caption=None,
        file_name="report.pdf",
    )
    assert "report.pdf" in result.text
    assert result.kind == "pdf_error"


async def test_summarize_pdf_empty_response_returns_placeholder():
    from echo_v2.services.media_summarizer import _summarize_pdf

    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content=""))]

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=fake_response)

    result = await _summarize_pdf(
        client=client,
        model="gpt-4o",
        pdf_bytes=b"%PDF-1.4 fake",
        caption=None,
        file_name="report.pdf",
    )
    assert "report.pdf" in result.text
    assert result.kind == "pdf_empty"


async def test_summarize_document_bytes_pdf_uses_vision_model():
    """PDFs are sent to the vision model, not the text model."""
    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content="a pdf summary"))]

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=fake_response)

    result = await summarize_document_bytes(
        client=client,
        vision_model="gpt-4o",
        text_model="gpt-4.1",
        document_bytes=b"%PDF-1.4 fake",
        mime_type="application/pdf",
        file_name="report.pdf",
        caption=None,
    )
    assert result.text == "a pdf summary"
    assert result.kind == "document"
    # Verify the vision model was used, not the text model.
    call_kwargs = client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "gpt-4o"


# --- OpenAIMediaSummarizer download error paths -----------------------------


async def test_openai_media_summarizer_image_download_failure_returns_placeholder():
    client = MagicMock()
    summarizer = OpenAIMediaSummarizer(client=client)
    with patch(
        "echo_v2.services.media_summarizer.download_media",
        new_callable=AsyncMock,
        side_effect=RuntimeError("network down"),
    ):
        result = await summarizer.summarize_media(
            download_url="https://example.com/img.jpeg",
            mime_type="image/jpeg",
            file_name="img.jpeg",
            message_type="image",
            caption=None,
        )
    assert result.text == "[image]"
    assert result.kind == "image_download_error"


async def test_openai_media_summarizer_document_download_failure_returns_placeholder():
    client = MagicMock()
    summarizer = OpenAIMediaSummarizer(client=client)
    with patch(
        "echo_v2.services.media_summarizer.download_media",
        new_callable=AsyncMock,
        side_effect=RuntimeError("network down"),
    ):
        result = await summarizer.summarize_media(
            download_url="https://example.com/report.pdf",
            mime_type="application/pdf",
            file_name="report.pdf",
            message_type="document",
            caption="the report",
        )
    assert "report.pdf" in result.text
    assert "the report" in result.text
    assert result.kind == "document_download_error"


async def test_openai_media_summarizer_document_text_path():
    """Text-like documents are downloaded and summarized via the text model."""
    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content="a text doc summary"))]

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=fake_response)

    summarizer = OpenAIMediaSummarizer(client=client)
    with patch(
        "echo_v2.services.media_summarizer.download_media",
        new_callable=AsyncMock,
        return_value=b"some plain text content",
    ):
        result = await summarizer.summarize_media(
            download_url="https://example.com/notes.txt",
            mime_type="text/plain",
            file_name="notes.txt",
            message_type="document",
            caption=None,
        )
    assert result.text == "a text doc summary"
    assert result.kind == "document"


# --- Link summarization edge cases -----------------------------------------


@patch("echo_v2.services.media_summarizer.httpx.AsyncClient")
async def test_summarize_link_empty_body_returns_placeholder(MockClient: MagicMock):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.headers = {"content-type": "text/plain"}
    mock_response.text = "   "
    mock_response.raise_for_status = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    MockClient.return_value = mock_client

    client = MagicMock()
    result = await summarize_link(
        client=client, model="gpt-4.1", url="https://example.com/empty"
    )
    assert "https://example.com/empty" in result.text
    assert result.kind == "link_empty"


@patch("echo_v2.services.media_summarizer.httpx.AsyncClient")
async def test_summarize_link_non_html_content(MockClient: MagicMock):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.headers = {"content-type": "text/plain"}
    mock_response.text = "just plain text content"
    mock_response.raise_for_status = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    MockClient.return_value = mock_client

    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content="plain text summary"))]

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=fake_response)

    result = await summarize_link(
        client=client, model="gpt-4.1", url="https://example.com/text"
    )
    assert result.text == "plain text summary"
    assert result.kind == "link"


# --- close() ----------------------------------------------------------------


async def test_openai_media_summarizer_close_no_http_client():
    """close() is a no-op when no http_client was injected."""
    summarizer = OpenAIMediaSummarizer(client=MagicMock())
    await summarizer.close()  # should not raise


async def test_openai_media_summarizer_close_closes_http_client():
    http_client = MagicMock()
    http_client.aclose = AsyncMock()
    summarizer = OpenAIMediaSummarizer(client=MagicMock(), http_client=http_client)
    await summarizer.close()
    http_client.aclose.assert_called_once()


# --- _summarize_text edge cases ---------------------------------------------


async def test_summarize_text_empty_response_returns_placeholder():
    from echo_v2.services.media_summarizer import _summarize_text

    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content=""))]

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=fake_response)

    result = await _summarize_text(
        client=client,
        model="gpt-4.1",
        text="some content",
        label="document",
        caption=None,
        file_name="notes.txt",
    )
    assert "notes.txt" in result.text
    assert result.kind == "document_empty"


async def test_summarize_text_api_failure_returns_placeholder():
    from echo_v2.services.media_summarizer import _summarize_text

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("api down"))

    result = await _summarize_text(
        client=client,
        model="gpt-4.1",
        text="some content",
        label="link",
        caption=None,
        file_name="https://example.com",
    )
    assert "https://example.com" in result.text
    assert result.kind == "link_summary_error"


# --- Placeholder helpers ----------------------------------------------------


def test_image_placeholder_with_caption():
    from echo_v2.services.media_summarizer import _image_placeholder

    assert _image_placeholder("my cat") == "[image: my cat]"


def test_image_placeholder_without_caption():
    from echo_v2.services.media_summarizer import _image_placeholder

    assert _image_placeholder(None) == "[image]"


def test_document_placeholder_with_file_and_caption():
    from echo_v2.services.media_summarizer import _document_placeholder

    assert _document_placeholder("the report", "report.pdf") == "[document: report.pdf, the report]"


def test_document_placeholder_with_no_parts():
    from echo_v2.services.media_summarizer import _document_placeholder

    assert _document_placeholder(None, None) == "[document]"


def test_video_placeholder_with_file_and_caption():
    from echo_v2.services.media_summarizer import _video_placeholder

    assert _video_placeholder("funny", "clip.mp4") == "[video: clip.mp4, funny]"


def test_video_placeholder_with_no_parts():
    from echo_v2.services.media_summarizer import _video_placeholder

    assert _video_placeholder(None, None) == "[video]"


# --- _strip_html title-not-in-text branch -----------------------------------


def test_strip_html_title_not_in_body_prepends_title():
    """When the title is not at the start of the body text, it is prepended."""
    html = "<html><head><title>Unique Title</title></head><body><p>completely different body</p></body></html>"
    text = _strip_html(html)
    assert text.startswith("Unique Title")
    assert "completely different body" in text


def test_strip_html_title_at_start_does_not_duplicate():
    """When the title is already at the start of the body, it is not prepended again.

    Note: the <title> tag content is included in the stripped text (it's
    inside the HTML), so the title appears once from the tag stripping.
    The function should not prepend it a second time.
    """
    html = "<html><head><title>My Page</title></head><body><p>My Page content here</p></body></html>"
    text = _strip_html(html)
    # The title appears once from the <title> tag and once from the body,
    # but the function should not add a third occurrence.
    assert text.count("My Page") == 2
