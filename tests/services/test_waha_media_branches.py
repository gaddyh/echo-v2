"""Additional branch coverage for WAHA media download and consumer loop."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from echo_v2.services.waha_media import (
    WahaMediaDownloadError,
    WahaMediaError,
    WahaMediaUnavailableError,
    WahaMediaUrlResolver,
    download_ciphertext,
)
from tests.services.test_waha_media import FakeInbox, _domain_message
from tests.services.test_waha_message_consumer import (
    FakeConnections,
    FakeIngestion,
    FakeRepository,
    _consumer,
    _stored_connection,
    _text_row,
)

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _install_mock_client(
    monkeypatch: pytest.MonkeyPatch, handler: httpx.MockTransport
) -> None:
    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return _REAL_ASYNC_CLIENT(transport=handler)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_download_ciphertext_streams_and_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"cipher" * 4)
    )
    _install_mock_client(monkeypatch, handler)
    data = await download_ciphertext("https://mmg.whatsapp.net/v/t62", max_bytes=100)
    assert data == b"cipher" * 4

    with pytest.raises(WahaMediaError, match="size cap"):
        await download_ciphertext("https://mmg.whatsapp.net/v/t62", max_bytes=10)


async def test_download_ciphertext_rejects_non_https() -> None:
    with pytest.raises(WahaMediaError, match="HTTPS"):
        await download_ciphertext("http://mmg.whatsapp.net/v/t62", max_bytes=10)


async def test_download_ciphertext_rejects_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "https://x/y"})
    )
    _install_mock_client(monkeypatch, handler)
    with pytest.raises(WahaMediaError, match="redirect"):
        await download_ciphertext("https://mmg.whatsapp.net/v/t62", max_bytes=10)


async def test_download_ciphertext_4xx_is_unavailable_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CDN 4xx = the object is evicted/expired: a distinct terminal class."""
    for status in (403, 404, 410):
        handler = httpx.MockTransport(lambda request, s=status: httpx.Response(s))
        _install_mock_client(monkeypatch, handler)
        with pytest.raises(WahaMediaUnavailableError, match="no longer available"):
            await download_ciphertext("https://mmg.whatsapp.net/v/t62", max_bytes=10)


async def test_download_ciphertext_5xx_stays_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = httpx.MockTransport(lambda request: httpx.Response(503))
    _install_mock_client(monkeypatch, handler)
    with pytest.raises(WahaMediaDownloadError, match="HTTP 503"):
        await download_ciphertext("https://mmg.whatsapp.net/v/t62", max_bytes=10)


async def test_download_ciphertext_errors_never_leak_signed_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signed = "https://mmg.whatsapp.net/v/t62.7117-24/abc.enc?oe=DEADBEEF&oh=secret&token=xyz"
    for status in (403, 503):
        handler = httpx.MockTransport(lambda request, s=status: httpx.Response(s))
        _install_mock_client(monkeypatch, handler)
        with pytest.raises(WahaMediaError) as excinfo:
            await download_ciphertext(signed, max_bytes=10)
        assert "mmg.whatsapp.net" not in str(excinfo.value)
        assert "token" not in str(excinfo.value)


async def test_download_ciphertext_transport_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    handler = httpx.MockTransport(_raise)
    _install_mock_client(monkeypatch, handler)
    with pytest.raises(WahaMediaDownloadError, match="ConnectError"):
        await download_ciphertext("https://mmg.whatsapp.net/v/t62", max_bytes=10)


async def test_resolver_rejects_malformed_reference(tmp_path) -> None:
    resolver = WahaMediaUrlResolver(FakeInbox(None), cache_dir=tmp_path / "m")
    with pytest.raises(WahaMediaError, match="invalid WAHA media reference"):
        await resolver.resolve(_domain_message("waha:only-two-parts"))


async def test_chained_resolver_ignores_non_string_reference() -> None:
    from echo_v2.services.waha_media import ChainedMediaUrlResolver

    chain = ChainedMediaUrlResolver({})
    assert await chain.resolve(_domain_message(None)) is None
    assert await chain.resolve(_domain_message(123)) is None


async def test_consumer_backlog_stats_failure_is_contained() -> None:
    class ExplodingRepository(FakeRepository):
        async def backlog_stats(self) -> tuple[int, int, float | None]:
            raise RuntimeError("db down")

    repo = ExplodingRepository()
    consumer = _consumer(repo, FakeConnections(None), FakeIngestion())
    await consumer._warn_on_stale_backlog(__import__("time").monotonic())
    # The warning path swallowed the exception; no crash.


async def test_run_loop_processes_until_empty_then_sleeps() -> None:
    repo = FakeRepository([_text_row()])
    consumer = _consumer(repo, FakeConnections(_stored_connection()), FakeIngestion())
    task = asyncio.create_task(consumer.run_loop())
    for _ in range(50):
        if repo.completed:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert len(repo.completed) == 1


async def test_run_loop_survives_repository_errors() -> None:
    class ExplodingRepository(FakeRepository):
        async def claim_pending(self, worker_id, **kwargs):
            raise RuntimeError("transient db error")

    repo = ExplodingRepository()
    consumer = _consumer(repo, FakeConnections(None), FakeIngestion())
    task = asyncio.create_task(consumer.run_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
