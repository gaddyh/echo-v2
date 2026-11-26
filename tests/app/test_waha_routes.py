"""Route tests for WAHA batch ingestion and worker registration.

Covers the wire contract (ACK triples, dup/updated counts), authentication
(fail-closed), streaming body limits, SHA-256 validation, within-batch
revision collapsing (only the persisted hash is ACKed), DB-failure behavior
(503, nothing acknowledged, no SQL details in logs), and log hygiene (no
payloads, phone numbers or tokens).

Repository behavior is faked here; transaction and concurrency semantics
live in tests/persistence/test_postgres_waha_event_inbox.py and
test_postgres_waha_workers.py (real Postgres Testcontainers).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from starlette.requests import Request

from echo_v2.app.waha_routes import _read_body_limited, build_waha_router
from echo_v2.domain.waha import (
    WahaBatchIngestResult,
    WahaInboxMessage,
    WahaWorkerRegistration,
)

TOKEN = "waha-secret"
WAHA_LOGGER = "echo_v2.app.waha_routes"


class _ListHandler(logging.Handler):
    """Capture handler immune to caplog pollution.

    Alembic's ``env.py`` runs ``fileConfig(alembic.ini)`` during
    migration-running tests, which disables every logger created before it
    (including this module's). Forcing ``disabled = False`` and attaching
    our own handler is the established workaround in this suite (see
    tests/observability/test_alert_checker.py).
    """

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _capture_logs(level: int = logging.INFO) -> Iterator[list[str]]:
    logger = logging.getLogger(WAHA_LOGGER)
    handler = _ListHandler()
    handler.setLevel(level)
    previous_level = logger.level
    logger.setLevel(level)
    logger.disabled = False  # alembic fileConfig may have disabled it
    logger.addHandler(handler)
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

SESSION = "gaddy-test"
MESSAGE_ID = "AC3B05AF332C19A84A8C9EF9C0B1FDD3"
GROUP_JID = "972544998652-1584868277@g.us"
PHONE_JID = "972544998652@c.us"


# --- fakes -------------------------------------------------------------------


class FakeWahaEventInbox:
    def __init__(self, fail: Exception | None = None) -> None:
        self.stored: dict[tuple[str, str], WahaInboxMessage] = {}
        self.revisions: dict[tuple[str, str], int] = {}
        self.fail = fail

    async def ingest_batch(self, batch_id: str, messages) -> WahaBatchIngestResult:
        if self.fail is not None:
            raise self.fail
        inserted = updated = duplicated = 0
        for m in messages:
            key = (m.session, m.message_id)
            existing = self.stored.get(key)
            if existing is None:
                self.stored[key] = replace(m, batch_id=batch_id)
                self.revisions[key] = 1
                inserted += 1
            elif existing.sha256 == m.sha256:
                duplicated += 1
            else:
                self.stored[key] = replace(m, batch_id=batch_id)
                self.revisions[key] = self.revisions.get(key, 1) + 1
                updated += 1
        return WahaBatchIngestResult(inserted, updated, duplicated)

    async def get(self, session: str, message_id: str) -> WahaInboxMessage | None:
        return self.stored.get((session, message_id))

    async def mark_processed(
        self, session: str, message_id: str, *, revision: int
    ) -> bool:
        return self.revisions.get((session, message_id)) == revision


class FakeWahaWorkers:
    def __init__(self, fail: Exception | None = None) -> None:
        self.registered: dict[str, WahaWorkerRegistration] = {}
        self.fail = fail

    async def register(self, registration: WahaWorkerRegistration) -> bool:
        if self.fail is not None:
            raise self.fail
        first = registration.worker_id not in self.registered
        self.registered[registration.worker_id] = registration
        return first

    async def list_workers(self, *, heartbeat_timeout_seconds: float = 90.0):
        return []


# --- payload helpers ----------------------------------------------------------


def _message_payload(
    *,
    session: str = SESSION,
    message_id: str = MESSAGE_ID,
    data: str = '{"text": "hello"}',
    sha256: str | None = None,
    from_me: int = 0,
    is_real: int = 0,
    jid: str = GROUP_JID,
    timestamp: Any = "2026-11-26 17:59:01+00:00",
    seq: int = 7,
    first_seen_at: Any = 1795718131.9,
) -> dict[str, Any]:
    return {
        "session": session,
        "id": message_id,
        "jid": jid,
        "timestamp": timestamp,
        "from_me": from_me,
        "is_real": is_real,
        "data": data,
        "sha256": sha256
        if sha256 is not None
        else hashlib.sha256(data.encode("utf-8")).hexdigest(),
        "seq": seq,
        "first_seen_at": first_seen_at,
    }


def _batch_payload(
    messages: list[dict[str, Any]],
    *,
    batch_id: str = "1795718136-14",
    sent_at: Any = 1795718136.42,
    bytes_field: Any = 513046,
) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "sent_at": sent_at,
        "bytes": bytes_field,
        "messages": messages,
    }


def _registration_payload(
    *,
    worker_id: str = "waha-01-8e3dfc4bab32",
    health: str = "ok",
    pending: int = 0,
    active_sessions: int = 1,
    max_sessions: int = 1,
    last_heartbeat: Any = 1795718136.42,
) -> dict[str, Any]:
    return {
        "worker_id": worker_id,
        "region": "uk-lon1",
        "endpoint": "http://127.0.0.1:3000",
        "max_sessions": max_sessions,
        "active_sessions": active_sessions,
        "version": "0.2",
        "health": health,
        "last_heartbeat": last_heartbeat,
        "pending": pending,
    }


def _client(
    inbox: FakeWahaEventInbox,
    workers: FakeWahaWorkers,
    *,
    token: str = TOKEN,
    max_messages: int = 200,
    max_body_bytes: int = 8 * 1024 * 1024,
) -> AsyncClient:
    app = FastAPI()
    app.include_router(
        build_waha_router(
            inbox=inbox,  # type: ignore[arg-type]
            workers=workers,  # type: ignore[arg-type]
            token=token,
            max_messages=max_messages,
            max_body_bytes=max_body_bytes,
        )
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# --- batch ingestion ----------------------------------------------------------


async def test_valid_batch_acks_exact_triples() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    messages = [
        _message_payload(),
        _message_payload(session="other-session", message_id="OTHER-1"),
    ]
    async with _client(inbox, workers) as client:
        r = await client.post(
            "/internal/waha/batch", json=_batch_payload(messages), headers=_auth()
        )
        assert r.status_code == 200
        body = r.json()
        assert body["batch_id"] == "1795718136-14"
        assert body["dup"] == 0
        assert body["updated"] == 0
        assert body["acked"] == [
            {"session": SESSION, "id": MESSAGE_ID, "sha256": messages[0]["sha256"]},
            {
                "session": "other-session",
                "id": "OTHER-1",
                "sha256": messages[1]["sha256"],
            },
        ]
    assert len(inbox.stored) == 2


async def test_group_and_phone_sent_messages_preserved() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    messages = [
        _message_payload(jid=GROUP_JID, from_me=0, is_real=0),
        _message_payload(
            message_id="SENT-1", jid=PHONE_JID, from_me=1, is_real=1, seq=8
        ),
    ]
    async with _client(inbox, workers) as client:
        r = await client.post(
            "/internal/waha/batch", json=_batch_payload(messages), headers=_auth()
        )
        assert r.status_code == 200
        assert len(r.json()["acked"]) == 2
    incoming = inbox.stored[(SESSION, MESSAGE_ID)]
    sent = inbox.stored[(SESSION, "SENT-1")]
    assert incoming.from_me == 0 and incoming.is_real == 0
    assert incoming.chat_jid == GROUP_JID
    assert sent.from_me == 1 and sent.is_real == 1
    assert sent.chat_jid == PHONE_JID


async def test_non_real_events_and_exact_data_string_preserved() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    raw = '{ "text" :  "שלום world" ,  "n": 1 }'
    messages = [_message_payload(data=raw, is_real=0)]
    async with _client(inbox, workers) as client:
        r = await client.post(
            "/internal/waha/batch", json=_batch_payload(messages), headers=_auth()
        )
        assert r.status_code == 200
    stored = inbox.stored[(SESSION, MESSAGE_ID)]
    assert stored.is_real == 0
    assert stored.data == raw


async def test_sha_mismatch_rejected_nothing_persisted() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    messages = [_message_payload(sha256="0" * 64)]
    async with _client(inbox, workers) as client:
        r = await client.post(
            "/internal/waha/batch", json=_batch_payload(messages), headers=_auth()
        )
        assert r.status_code == 400
        assert "sha256" in r.json()["detail"]
    assert inbox.stored == {}


@pytest.mark.parametrize(
    "payload",
    [
        _batch_payload([], batch_id=""),
        _batch_payload([], sent_at="not-a-number"),
        _batch_payload([], bytes_field="many"),
        _batch_payload([], bytes_field=True),
        {"sent_at": 1.0, "messages": []},
        _batch_payload([_message_payload(session="")]),
        _batch_payload([_message_payload(message_id="")]),
        _batch_payload([_message_payload(jid="")]),
        _batch_payload([_message_payload(timestamp="2026-11-26 17:59:01")]),
        _batch_payload([_message_payload(timestamp="yesterday")]),
        _batch_payload([_message_payload(timestamp=123, sha256="0" * 64)]),
        _batch_payload([_message_payload(from_me=2)]),
        _batch_payload([_message_payload(from_me=True)]),
        _batch_payload([_message_payload(from_me="0")]),
        _batch_payload([_message_payload(is_real=5)]),
        _batch_payload([_message_payload(data="")]),
        _batch_payload([_message_payload(data=123, sha256="0" * 64)]),
        _batch_payload([_message_payload(sha256="tooshort")]),
        _batch_payload([_message_payload(sha256="Z" * 64)]),
        _batch_payload([_message_payload(seq="7")]),
        _batch_payload([_message_payload(first_seen_at="now")]),
        _batch_payload([_message_payload(first_seen_at=1e18)]),
        _batch_payload(["not-an-object"]),
    ],
)
async def test_invalid_payloads_rejected(payload: dict[str, Any]) -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    async with _client(inbox, workers) as client:
        r = await client.post(
            "/internal/waha/batch", json=payload, headers=_auth()
        )
        assert r.status_code == 400
    assert inbox.stored == {}


async def test_empty_batch_is_valid_no_op() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    async with _client(inbox, workers) as client:
        r = await client.post(
            "/internal/waha/batch", json=_batch_payload([]), headers=_auth()
        )
        assert r.status_code == 200
        assert r.json() == {
            "batch_id": "1795718136-14",
            "acked": [],
            "dup": 0,
            "updated": 0,
        }


async def test_invalid_json_rejected() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    async with _client(inbox, workers) as client:
        r = await client.post(
            "/internal/waha/batch",
            content=b"{not-json",
            headers={**_auth(), "content-type": "application/json"},
        )
        assert r.status_code == 400


async def test_batch_payload_must_be_object() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    async with _client(inbox, workers) as client:
        r = await client.post(
            "/internal/waha/batch", json=[1, 2], headers=_auth()
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "payload must be an object"


async def test_batch_messages_must_be_list() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    payload = {"batch_id": "b-1", "sent_at": 1.0, "messages": "nope"}
    async with _client(inbox, workers) as client:
        r = await client.post("/internal/waha/batch", json=payload, headers=_auth())
        assert r.status_code == 400
        assert r.json()["detail"] == "messages must be a list"


async def test_oversized_body_rejected_413() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    async with _client(inbox, workers, max_body_bytes=256) as client:
        r = await client.post(
            "/internal/waha/batch",
            content=b"x" * 1024,
            headers={**_auth(), "content-type": "application/json"},
        )
        assert r.status_code == 413
    assert inbox.stored == {}


async def test_too_many_messages_rejected_413() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    messages = [
        _message_payload(message_id=f"M-{i}", seq=i) for i in range(3)
    ]
    async with _client(inbox, workers, max_messages=2) as client:
        r = await client.post(
            "/internal/waha/batch", json=_batch_payload(messages), headers=_auth()
        )
        assert r.status_code == 413
    assert inbox.stored == {}


async def test_identical_resend_deduplicated_and_reacked() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    messages = [_message_payload()]
    payload = _batch_payload(messages)
    async with _client(inbox, workers) as client:
        first = await client.post("/internal/waha/batch", json=payload, headers=_auth())
        assert first.status_code == 200
        assert first.json()["dup"] == 0
        # Simulated lost ACK: agent resends the identical batch.
        second = await client.post(
            "/internal/waha/batch", json=payload, headers=_auth()
        )
        assert second.status_code == 200
        assert second.json() == first.json() | {"dup": 1}
        assert second.json()["acked"] == first.json()["acked"]
    assert len(inbox.stored) == 1


async def test_duplicate_message_in_different_batch() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    async with _client(inbox, workers) as client:
        r1 = await client.post(
            "/internal/waha/batch",
            json=_batch_payload([_message_payload()], batch_id="b-1"),
            headers=_auth(),
        )
        r2 = await client.post(
            "/internal/waha/batch",
            json=_batch_payload([_message_payload()], batch_id="b-2"),
            headers=_auth(),
        )
        assert r1.status_code == r2.status_code == 200
        assert r2.json()["dup"] == 1
        assert r2.json()["acked"] == r1.json()["acked"]
    assert len(inbox.stored) == 1


async def test_same_message_id_in_different_sessions() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    messages = [
        _message_payload(session="session-a"),
        _message_payload(session="session-b"),
    ]
    async with _client(inbox, workers) as client:
        r = await client.post(
            "/internal/waha/batch", json=_batch_payload(messages), headers=_auth()
        )
        assert r.status_code == 200
        assert len(r.json()["acked"]) == 2
    assert (SESSION, MESSAGE_ID) not in inbox.stored
    assert ("session-a", MESSAGE_ID) in inbox.stored
    assert ("session-b", MESSAGE_ID) in inbox.stored


async def test_in_batch_different_hashes_only_persisted_hash_acked() -> None:
    """Same (session, id) with different hashes: the discarded revision is
    never ACKed — only the hash that was actually persisted (last one)."""
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    older = _message_payload(data='{"rev": 1}')
    newer = _message_payload(data='{"rev": 2}', seq=8)
    async with _client(inbox, workers) as client:
        r = await client.post(
            "/internal/waha/batch",
            json=_batch_payload([older, newer]),
            headers=_auth(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["acked"] == [
            {"session": SESSION, "id": MESSAGE_ID, "sha256": newer["sha256"]}
        ]
        assert older["sha256"] not in [a["sha256"] for a in body["acked"]]
        assert body["dup"] == 0
        assert body["updated"] == 0
    stored = inbox.stored[(SESSION, MESSAGE_ID)]
    assert stored.sha256 == newer["sha256"]
    assert stored.data == '{"rev": 2}'


async def test_in_batch_same_hash_counted_as_duplicate() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    message = _message_payload()
    async with _client(inbox, workers) as client:
        r = await client.post(
            "/internal/waha/batch",
            json=_batch_payload([message, dict(message)]),
            headers=_auth(),
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["acked"]) == 1
        assert body["dup"] == 1
    assert len(inbox.stored) == 1


async def test_missing_and_invalid_auth_rejected() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    async with _client(inbox, workers) as client:
        no_header = await client.post(
            "/internal/waha/batch", json=_batch_payload([_message_payload()])
        )
        assert no_header.status_code == 401

        bad_token = await client.post(
            "/internal/waha/batch",
            json=_batch_payload([_message_payload()]),
            headers=_auth("wrong-token"),
        )
        assert bad_token.status_code == 401

        not_bearer = await client.post(
            "/internal/waha/batch",
            json=_batch_payload([_message_payload()]),
            headers={"authorization": f"Basic {TOKEN}"},
        )
        assert not_bearer.status_code == 401
    assert inbox.stored == {}


async def test_unset_token_fails_closed_503() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    async with _client(inbox, workers, token="") as client:
        r = await client.post(
            "/internal/waha/batch",
            json=_batch_payload([_message_payload()]),
            headers=_auth("anything"),
        )
        assert r.status_code == 503
    assert inbox.stored == {}


async def test_db_failure_returns_503_without_acking() -> None:
    inbox = FakeWahaEventInbox(
        fail=OperationalError(
            "INSERT INTO waha_event_inbox ...",
            {"data": "SECRET-PAYLOAD-MARKER"},
            Exception("SECRET-PAYLOAD-MARKER connection refused"),
        )
    )
    workers = FakeWahaWorkers()
    with _capture_logs(logging.ERROR) as messages:
        async with _client(inbox, workers) as client:
            r = await client.post(
                "/internal/waha/batch",
                json=_batch_payload([_message_payload()]),
                headers=_auth(),
            )
            assert r.status_code == 503
            assert "acked" not in r.json()
    assert inbox.stored == {}
    # Only the exception type is logged — never SQL text or bind parameters.
    assert any("error_type=OperationalError" in m for m in messages)
    assert all("SECRET-PAYLOAD-MARKER" not in m for m in messages)


async def test_waha_db_failure_does_not_affect_other_routes() -> None:
    inbox = FakeWahaEventInbox(fail=OperationalError("s", {}, Exception("db down")))
    workers = FakeWahaWorkers()
    async with _client(inbox, workers) as client:
        failed = await client.post(
            "/internal/waha/batch",
            json=_batch_payload([_message_payload()]),
            headers=_auth(),
        )
        assert failed.status_code == 503
        # The same app keeps serving other routes (per-request isolation).
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}


async def test_no_sensitive_payloads_in_logs() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    secret_data = '{"SECRET-CONTENTS": "SENSITIVE-PAYLOAD-XYZ"}'
    messages = [_message_payload(data=secret_data)]
    with _capture_logs(logging.INFO) as logged:
        async with _client(inbox, workers) as client:
            r = await client.post(
                "/internal/waha/batch", json=_batch_payload(messages), headers=_auth()
            )
            assert r.status_code == 200
    combined = "\n".join(logged)
    assert "SENSITIVE-PAYLOAD-XYZ" not in combined
    assert GROUP_JID not in combined  # phone numbers / JIDs never logged
    assert TOKEN not in combined  # tokens never logged
    assert "waha batch ingested" in combined


# --- worker registration ------------------------------------------------------


async def test_first_registration_persisted_and_logged() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    with _capture_logs(logging.INFO) as logged:
        async with _client(inbox, workers) as client:
            r = await client.post(
                "/internal/waha/register",
                json=_registration_payload(),
                headers=_auth(),
            )
            assert r.status_code == 200
            assert r.json() == {"ok": True}
    assert "waha-01-8e3dfc4bab32" in workers.registered
    assert any("waha worker registered" in m for m in logged)
    combined = "\n".join(logged)
    assert "http://127.0.0.1:3000" not in combined  # endpoints not logged


async def test_recurring_heartbeat_not_logged_at_info() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    with _capture_logs(logging.INFO) as logged:
        async with _client(inbox, workers) as client:
            first = await client.post(
                "/internal/waha/register", json=_registration_payload(), headers=_auth()
            )
            second = await client.post(
                "/internal/waha/register",
                json=_registration_payload(health="degraded", pending=4),
                headers=_auth(),
            )
            assert first.status_code == second.status_code == 200
            assert second.json() == {"ok": True}
    assert sum("waha worker registered" in m for m in logged) == 1
    assert not any("heartbeat" in m for m in logged)
    assert workers.registered["waha-01-8e3dfc4bab32"].health == "degraded"
    assert workers.registered["waha-01-8e3dfc4bab32"].pending == 4


async def test_registration_validation_errors() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    async with _client(inbox, workers) as client:
        bad_health = await client.post(
            "/internal/waha/register",
            json=_registration_payload(health="on-fire"),
            headers=_auth(),
        )
        assert bad_health.status_code == 400
        negative = await client.post(
            "/internal/waha/register",
            json=_registration_payload(max_sessions=-1),
            headers=_auth(),
        )
        assert negative.status_code == 400
        missing = await client.post(
            "/internal/waha/register",
            json={"region": "uk-lon1"},
            headers=_auth(),
        )
        assert missing.status_code == 400
        bad_epoch = await client.post(
            "/internal/waha/register",
            json=_registration_payload(last_heartbeat="soon"),
            headers=_auth(),
        )
        assert bad_epoch.status_code == 400

        overflowing_epoch = await client.post(
            "/internal/waha/register",
            json=_registration_payload(last_heartbeat=1e18),
            headers=_auth(),
        )
        assert overflowing_epoch.status_code == 400

        payload_not_object = await client.post(
            "/internal/waha/register", json=[1], headers=_auth()
        )
        assert payload_not_object.status_code == 400
    assert workers.registered == {}


async def test_register_invalid_json_rejected() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    async with _client(inbox, workers) as client:
        r = await client.post(
            "/internal/waha/register",
            content=b"{broken",
            headers={**_auth(), "content-type": "application/json"},
        )
        assert r.status_code == 400


async def test_registration_auth_and_db_failure() -> None:
    inbox, workers = FakeWahaEventInbox(), FakeWahaWorkers()
    async with _client(inbox, workers) as client:
        unauthenticated = await client.post(
            "/internal/waha/register", json=_registration_payload()
        )
        assert unauthenticated.status_code == 401

        unauthorized = await client.post(
            "/internal/waha/register",
            json=_registration_payload(),
            headers=_auth("wrong"),
        )
        assert unauthorized.status_code == 401

    async with _client(FakeWahaEventInbox(), FakeWahaWorkers(), token="") as client:
        unconfigured = await client.post(
            "/internal/waha/register",
            json=_registration_payload(),
            headers=_auth("irrelevant"),
        )
        assert unconfigured.status_code == 503

    failing = FakeWahaWorkers(fail=OperationalError("s", {}, Exception("db down")))
    async with _client(FakeWahaEventInbox(), failing) as client:
        r = await client.post(
            "/internal/waha/register",
            json=_registration_payload(),
            headers=_auth(),
        )
        assert r.status_code == 503
    assert failing.registered == {}


# --- streaming body limit ------------------------------------------------------


def _streaming_request(chunks: list[bytes], headers: list[tuple[bytes, bytes]] | None = None):
    queue = list(chunks)

    async def receive():
        if queue:
            chunk = queue.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(queue)}
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/internal/waha/batch",
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
    }
    return Request(scope, receive), queue


async def test_body_limit_enforced_during_streaming() -> None:
    request, queue = _streaming_request([b"a" * 60, b"b" * 60, b"c" * 60])
    with pytest.raises(HTTPException) as exc_info:
        await _read_body_limited(request, limit=100)
    assert exc_info.value.status_code == 413
    # The third chunk was never consumed — enforcement aborted the stream
    # as soon as the cumulative size crossed the limit.
    assert len(queue) == 1


async def test_body_limit_rejects_oversized_content_length() -> None:
    request, queue = _streaming_request(
        [b"a" * 60], headers=[(b"content-length", b"999999")]
    )
    with pytest.raises(HTTPException) as exc_info:
        await _read_body_limited(request, limit=100)
    assert exc_info.value.status_code == 413
    assert len(queue) == 1  # body never consumed


async def test_body_limit_rejects_non_numeric_content_length() -> None:
    request, _ = _streaming_request(
        [b"a" * 60], headers=[(b"content-length", b"not-a-number")]
    )
    with pytest.raises(HTTPException) as exc_info:
        await _read_body_limited(request, limit=100)
    assert exc_info.value.status_code == 400


async def test_client_disconnect_during_body_read() -> None:
    async def receive():
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/internal/waha/batch",
        "query_string": b"",
        "root_path": "",
        "headers": [],
    }
    request = Request(scope, receive)
    with pytest.raises(HTTPException) as exc_info:
        await _read_body_limited(request, limit=100)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "client disconnected"


async def test_body_limit_allows_body_within_limit() -> None:
    request, queue = _streaming_request([b"hello ", b"world"])
    body = await _read_body_limited(request, limit=100)
    assert body == b"hello world"
    assert queue == []
