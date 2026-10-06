"""Tests for the deterministic Guard demo replay routes."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from echo_v2.app.guard_demo_routes import build_guard_demo_router

EXPECTED_SCENARIOS = [
    "unknown_contact_escalation_001",
    "suspicious_contact_002_known_activity_coordinator_negative",
    "teasing_vs_bullying_002_positive",
    "distress_001_hopelessness_and_help_request_positive",
    "child_sexual_exploitation_002_intimate_image_sextortion",
]
EXPECTED_HEBREW_SCENARIOS = [
    "gold_parent_promise_01_stranger_getting_closer",
    "gold_parent_promise_02_group_turns_on_child",
    "gold_parent_promise_03_quiet_social_exclusion",
    "gold_parent_promise_04_embarrassing_video_spreads",
    "gold_parent_promise_05_no_is_not_respected",
    "gold_parent_promise_06_bad_day_becomes_more",
]


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(build_guard_demo_router())
    return app


@pytest.mark.asyncio
async def test_demo_page_serves_replay_shell_with_security_headers() -> None:
    async with _client(_make_app()) as client:
        response = await client.get("/demo/guard")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "See danger emerge before it becomes obvious" in response.text
    assert 'id="start"' in response.text
    assert 'id="next"' in response.text
    assert 'href="/demo/guard/he"' in response.text
    assert "function playNextSnapshot()" in response.text
    assert "state.snapshotIndex>=scenario.snapshots.length-1" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in response.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_hebrew_demo_page_serves_rtl_replay_shell() -> None:
    async with _client(_make_app()) as client:
        response = await client.get("/demo/guard/he")

    assert response.status_code == 200
    assert '<html lang="he" dir="rtl">' in response.text
    assert "התחלת הדגמה" in response.text
    assert "המצב הנוכחי" in response.text
    assert "fetch('/api/demo/guard?lang=he')" in response.text
    assert 'href="/demo/guard"' in response.text
    assert "function playNextSnapshot()" in response.text
    assert response.headers["content-security-policy"].startswith("default-src 'self'")


@pytest.mark.asyncio
async def test_demo_data_contains_selected_scenarios_in_presentation_order() -> None:
    async with _client(_make_app()) as client:
        response = await client.get("/api/demo/guard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "echo_guard_demo_replay"
    assert [scenario["id"] for scenario in payload["scenarios"]] == EXPECTED_SCENARIOS
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_hebrew_demo_data_uses_hebrew_fixture() -> None:
    async with _client(_make_app()) as client:
        response = await client.get("/api/demo/guard?lang=he")

    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == 4
    assert payload["kind"] == "echo_guard_demo_gold_set"
    assert [scenario["id"] for scenario in payload["scenarios"]] == EXPECTED_HEBREW_SCENARIOS
    assert payload["scenarios"][0]["title"] == "מישהו חדש מתחיל להתקרב"
    assert response.headers["cache-control"] == "no-store"


def test_demo_fixture_snapshots_are_ordered_and_policy_fields_are_separate() -> None:
    from echo_v2.app.guard_demo_routes import _load_fixture

    payload = _load_fixture()
    total_snapshots = 0
    for scenario in payload["scenarios"]:
        messages = scenario["messages"]
        positions = {message["id"]: index for index, message in enumerate(messages)}
        snapshots = scenario["snapshots"]
        previous_position = -1
        cumulative_signals: set[str] = set()
        for snapshot in snapshots:
            after_id = snapshot["after_message_id"]
            assert after_id in positions
            position = positions[after_id]
            assert position > previous_position
            previous_position = position
            visible_ids = {message["id"] for message in messages[: position + 1]}
            assert set(snapshot["raw"]["evidence_message_ids"]) <= visible_ids
            assert set(snapshot["cumulative"]["evidence_message_ids"]) <= visible_ids
            current_signals = set(snapshot["cumulative"]["signals"])
            assert cumulative_signals <= current_signals
            cumulative_signals = current_signals
            assert snapshot["decision"] in {"none", "watch", "concerning", "urgent"}
            assert isinstance(snapshot["evaluation_pass"], bool)
            assert isinstance(snapshot["should_alert"], bool)
            assert snapshot["alert_label"] in {"Not yet", "Notify parent"}
            assert "should_alert" not in snapshot["raw"]
            total_snapshots += 1

    assert total_snapshots == 17
