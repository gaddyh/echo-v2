"""Tests for the landing page + waitlist signup routes."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from echo_v2.app.landing_routes import build_landing_router
from echo_v2.persistence.waitlist import InMemoryWaitlistRepository

pytestmark = pytest.mark.asyncio


def _make_app() -> tuple[FastAPI, InMemoryWaitlistRepository]:
    repo = InMemoryWaitlistRepository()
    app = FastAPI()
    app.include_router(build_landing_router(waitlist_repo=repo))
    return app, repo


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


# --- GET / -------------------------------------------------------------------


async def test_landing_page_serves_html():
    app, _ = _make_app()
    async with _client(app) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Echo" in resp.text
    assert "רשימת המתנה" in resp.text
    # Security headers.
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert "default-src 'self'" in resp.headers.get("content-security-policy", "")


# --- POST /api/waitlist ------------------------------------------------------


async def test_waitlist_signup_success():
    app, repo = _make_app()
    async with _client(app) as client:
        resp = await client.post(
            "/api/waitlist",
            json={"name": "דנה לוי", "phone": "0546610653"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    signups = await repo.list_all()
    assert len(signups) == 1
    assert signups[0].name == "דנה לוי"
    # Phone normalized to E.164.
    assert signups[0].phone_number == "+972546610653"


async def test_waitlist_signup_normalizes_phone_variants():
    """0546610653, 972546610653, +972546610653 all collapse to one row."""
    app, repo = _make_app()
    async with _client(app) as client:
        for phone in ["0546610653", "972546610653", "+972-54-661-0653"]:
            resp = await client.post(
                "/api/waitlist",
                json={"name": "דנה", "phone": phone},
            )
            assert resp.status_code == 200
    signups = await repo.list_all()
    assert len(signups) == 1


async def test_waitlist_duplicate_returns_same_response():
    """A duplicate signup must be indistinguishable from a new one."""
    app, _ = _make_app()
    async with _client(app) as client:
        first = await client.post(
            "/api/waitlist", json={"name": "דנה", "phone": "0546610653"},
        )
        second = await client.post(
            "/api/waitlist", json={"name": "אחר", "phone": "0546610653"},
        )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


async def test_waitlist_invalid_phone_returns_422():
    app, repo = _make_app()
    async with _client(app) as client:
        resp = await client.post(
            "/api/waitlist", json={"name": "דנה", "phone": "123456"},
        )
    assert resp.status_code == 422
    assert await repo.list_all() == []


async def test_waitlist_empty_name_returns_422():
    app, _ = _make_app()
    async with _client(app) as client:
        resp = await client.post(
            "/api/waitlist", json={"name": "   ", "phone": "0546610653"},
        )
    assert resp.status_code == 422


async def test_waitlist_missing_fields_return_422():
    app, _ = _make_app()
    async with _client(app) as client:
        resp = await client.post("/api/waitlist", json={"name": "דנה"})
    assert resp.status_code == 422


async def test_waitlist_rate_limit():
    app, _ = _make_app()
    async with _client(app) as client:
        # 10 allowed per window; the 11th is rejected.
        for i in range(10):
            resp = await client.post(
                "/api/waitlist",
                json={"name": "דנה", "phone": f"05012345{i:02d}"},
            )
            assert resp.status_code in (200, 422)
        resp = await client.post(
            "/api/waitlist", json={"name": "דנה", "phone": "0509999999"},
        )
    assert resp.status_code == 429


# --- WTP, counter, OG image --------------------------------------------------


async def test_waitlist_signup_accepts_wtp():
    app, repo = _make_app()
    async with _client(app) as client:
        resp = await client.post(
            "/api/waitlist",
            json={"name": "דנה", "phone": "0546610653", "wtp": "30_70"},
        )
    assert resp.status_code == 200
    signups = await repo.list_all()
    assert signups[0].willingness_to_pay == "30_70"


async def test_waitlist_signup_without_wtp_defaults_null():
    app, repo = _make_app()
    async with _client(app) as client:
        resp = await client.post(
            "/api/waitlist",
            json={"name": "דנה", "phone": "0546610653"},
        )
    assert resp.status_code == 200
    signups = await repo.list_all()
    assert signups[0].willingness_to_pay is None


async def test_waitlist_signup_rejects_invalid_wtp():
    app, _ = _make_app()
    async with _client(app) as client:
        resp = await client.post(
            "/api/waitlist",
            json={"name": "דנה", "phone": "0546610653", "wtp": "bogus"},
        )
    assert resp.status_code == 422


async def test_landing_page_has_og_tags_and_scarcity():
    app, _ = _make_app()
    async with _client(app) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert 'og:title' in html
    assert 'og:image' in html
    assert '{{BASE_URL}}' not in html  # template substituted
    # Scarcity messaging is always present.
    assert "50 מקומות" in html
    # Counter is hidden below the display threshold.
    assert "{{COUNTER}}" not in html
    assert "כבר ברשימה" not in html


async def test_landing_page_shows_counter_above_threshold():
    """When the list crosses the threshold, the live counter appears."""
    from echo_v2.app import landing_routes

    app, repo = _make_app()
    # Stuff the repo past the threshold.
    for i in range(landing_routes._COUNTER_DISPLAY_THRESHOLD):
        await repo.add(name=f"u{i}", phone_number=f"+97250100{i:04d}")
    async with _client(app) as client:
        resp = await client.get("/")
    assert "כבר ברשימה" in resp.text


async def test_og_image_endpoint_serves_png():
    app, _ = _make_app()
    async with _client(app) as client:
        resp = await client.get("/og.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


# --- Notifier ----------------------------------------------------------------


async def test_notifier_fires_on_new_signup():
    calls: list[dict] = []

    class FakeNotifier:
        async def notify(self, *, name, phone, willingness_to_pay=None):
            calls.append(
                {"name": name, "phone": phone, "wtp": willingness_to_pay}
            )

    repo = InMemoryWaitlistRepository()
    app = FastAPI()
    app.include_router(
        build_landing_router(waitlist_repo=repo, notifier=FakeNotifier())
    )
    async with _client(app) as client:
        resp = await client.post(
            "/api/waitlist",
            json={"name": "דנה", "phone": "0546610653", "wtp": "30_70"},
        )
    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0] == {
        "name": "דנה",
        "phone": "+972546610653",
        "wtp": "30_70",
    }


async def test_notifier_silent_on_duplicate():
    calls: list[dict] = []

    class FakeNotifier:
        async def notify(self, *, name, phone, willingness_to_pay=None):
            calls.append({"name": name})

    repo = InMemoryWaitlistRepository()
    app = FastAPI()
    app.include_router(
        build_landing_router(waitlist_repo=repo, notifier=FakeNotifier())
    )
    async with _client(app) as client:
        await client.post(
            "/api/waitlist", json={"name": "דנה", "phone": "0546610653"},
        )
        await client.post(
            "/api/waitlist", json={"name": "אחר", "phone": "0546610653"},
        )
    # Only the first (new) signup triggers a notification.
    assert len(calls) == 1
    assert calls[0]["name"] == "דנה"


async def test_notifier_failure_does_not_break_signup():
    class BoomNotifier:
        async def notify(self, *, name, phone, willingness_to_pay=None):
            raise RuntimeError("boom")

    repo = InMemoryWaitlistRepository()
    app = FastAPI()
    app.include_router(
        build_landing_router(waitlist_repo=repo, notifier=BoomNotifier())
    )
    async with _client(app) as client:
        resp = await client.post(
            "/api/waitlist", json={"name": "דנה", "phone": "0546610653"},
        )
    assert resp.status_code == 200
    signups = await repo.list_all()
    assert len(signups) == 1
