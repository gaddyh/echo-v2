"""Route tests for connector ops-alert ingestion and admin browsing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from echo_v2.app.ops_alert_routes import build_ops_alert_router
from echo_v2.domain.ops_digest import OpsDigest
from echo_v2.persistence.waiting_list_tokens import InMemoryWaitingListSessionRepository
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

ALERT_TOKEN = "ops-secret"

NOW = datetime(2026, 11, 19, 21, 0, 0, tzinfo=timezone.utc)


def _digest_payload(
    *,
    attention: bool = False,
    one_liner: str = "שעה שקטה",
    still_open: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "severity": "high" if attention else "info",
        "title": one_liner,
        "body": json.dumps(
            {
                "attention": attention,
                "happened": [
                    {"title": "Deploy handover", "severity": "info", "detail": "ok"}
                ],
                "resolved": [],
                "still_open": still_open or [],
                "one_liner": one_liner,
            }
        ),
        "source": "connector",
        "worker_id": "worker-a",
        "timestamp": NOW.isoformat(),
        "window": {
            "from": (NOW - timedelta(hours=1)).isoformat(),
            "to": NOW.isoformat(),
        },
        "metrics": {
            "counters": {"publish_dropped": 0, "recovery_scheduled": 4},
            "peak_recovery_per_min": 2,
        },
    }


class FakeOpsDigestRepository:
    def __init__(self) -> None:
        self.recorded: list[OpsDigest] = []
        self._stored: list[OpsDigest] = []

    async def record(self, digest: OpsDigest) -> None:
        self.recorded.append(digest)
        if not any(d.digest_key == digest.digest_key for d in self._stored):
            self._stored.append(digest)

    async def list_since(
        self, *, since: datetime, limit: int = 100
    ) -> list[OpsDigest]:
        return [
            d for d in self._stored if d.received_at is not None and d.received_at >= since
        ][:limit]

    async def latest(self) -> OpsDigest | None:
        return self._stored[-1] if self._stored else None


def _client(
    repo: FakeOpsDigestRepository,
    tokens: WaitingListTokenService,
    *,
    admin_user_ids: set[str] | None = None,
    alert_token: str = ALERT_TOKEN,
) -> AsyncClient:
    app = FastAPI()
    app.include_router(
        build_ops_alert_router(
            repository=repo,  # type: ignore[arg-type]
            token_service=tokens,
            admin_user_ids=admin_user_ids or set(),
            alert_token=alert_token,
        )
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


def _auth(token: str = ALERT_TOKEN) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def test_ingest_persists_digest() -> None:
    repo = FakeOpsDigestRepository()
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    async with _client(repo, tokens) as client:
        r = await client.post(
            "/internal/ops-alerts", json=_digest_payload(), headers=_auth()
        )
        assert r.status_code == 204
    assert len(repo.recorded) == 1
    digest = repo.recorded[0]
    assert digest.worker_id == "worker-a"
    assert digest.attention is False
    assert digest.severity == "info"
    assert digest.one_liner == "שעה שקטה"
    assert digest.metrics is not None
    assert digest.metrics["counters"]["recovery_scheduled"] == 4
    assert digest.window_to == NOW
    assert digest.window_from == NOW - timedelta(hours=1)
    assert digest.digest_key == f"worker-a:{NOW.isoformat()}"


async def test_ingest_marks_attention_from_digest() -> None:
    repo = FakeOpsDigestRepository()
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    async with _client(repo, tokens) as client:
        r = await client.post(
            "/internal/ops-alerts",
            json=_digest_payload(attention=True, one_liner="publish drops"),
            headers=_auth(),
        )
        assert r.status_code == 204
    assert repo.recorded[0].attention is True
    assert repo.recorded[0].severity == "high"


async def test_ingest_rejects_bad_token() -> None:
    repo = FakeOpsDigestRepository()
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    async with _client(repo, tokens) as client:
        r = await client.post(
            "/internal/ops-alerts", json=_digest_payload(), headers=_auth("wrong")
        )
        assert r.status_code == 401
        r = await client.post("/internal/ops-alerts", json=_digest_payload())
        assert r.status_code == 401
    assert repo.recorded == []


async def test_ingest_rejects_misconfigured_token() -> None:
    repo = FakeOpsDigestRepository()
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    async with _client(repo, tokens, alert_token="") as client:
        r = await client.post(
            "/internal/ops-alerts", json=_digest_payload(), headers=_auth()
        )
        assert r.status_code == 503


async def test_ingest_rejects_invalid_payloads() -> None:
    repo = FakeOpsDigestRepository()
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    async with _client(repo, tokens) as client:
        r = await client.post(
            "/internal/ops-alerts", content=b"not json", headers=_auth()
        )
        assert r.status_code == 400

        missing = _digest_payload()
        del missing["worker_id"]
        r = await client.post("/internal/ops-alerts", json=missing, headers=_auth())
        assert r.status_code == 400

        bad_body = _digest_payload()
        bad_body["body"] = "not json"
        r = await client.post(
            "/internal/ops-alerts", json=bad_body, headers=_auth()
        )
        assert r.status_code == 400

        no_one_liner = _digest_payload()
        no_one_liner["body"] = json.dumps({"attention": False})
        r = await client.post(
            "/internal/ops-alerts", json=no_one_liner, headers=_auth()
        )
        assert r.status_code == 400
    assert repo.recorded == []


async def test_list_digests_requires_admin() -> None:
    repo = FakeOpsDigestRepository()
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    async with _client(repo, tokens, admin_user_ids={"admin"}) as client:
        r = await client.get("/api/admin/guard/ops/digests")
        assert r.status_code == 401


async def test_list_digests_returns_history() -> None:
    repo = FakeOpsDigestRepository()
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="admin")
    await repo.record(
        OpsDigest(
            digest_key="worker-a:old",
            worker_id="worker-a",
            source="connector",
            severity="info",
            attention=False,
            one_liner="quiet",
            digest={"still_open": [], "happened": []},
            received_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        )
    )
    async with _client(repo, tokens, admin_user_ids={"admin"}) as client:
        r = await client.get(
            "/api/admin/guard/ops/digests",
            cookies={"wls": session_id},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["period"] == "24h"
        assert len(data["digests"]) == 1
        assert data["digests"][0]["one_liner"] == "quiet"
        assert data["digests"][0]["still_open"] == []

        r = await client.get(
            "/api/admin/guard/ops/digests",
            cookies={"wls": session_id},
            params={"period": "bogus"},
        )
        assert r.status_code == 400
