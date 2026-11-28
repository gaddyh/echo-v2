"""Tests for the WAHA message consumer.

Fakes the inbox repository, connections repository, and ingestion service;
mirrors tests/services/test_baileys_message_consumer.py. Also covers the
backlog-age warning (media CDN URLs expire).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from echo_v2.domain.waha import ClaimedWahaMessage, WahaInboxMessage
from echo_v2.persistence.whatsapp_connections import StoredConnection
from echo_v2.ports.whatsapp import ConnectionRef, ConnectionStatus, MessageDirection
from echo_v2.services.waha_message_consumer import WahaMessageConsumer

NOW = datetime(2026, 11, 26, 22, 5, 12, tzinfo=timezone.utc)
CONSUMER_LOGGER = "echo_v2.services.waha_message_consumer"


class _ListHandler(logging.Handler):
    """Capture handler immune to caplog pollution (alembic fileConfig)."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _capture_logs(level: int = logging.WARNING) -> Iterator[list[str]]:
    logger = logging.getLogger(CONSUMER_LOGGER)
    handler = _ListHandler()
    handler.setLevel(level)
    previous_level = logger.level
    logger.setLevel(level)
    logger.disabled = False
    logger.addHandler(handler)
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _inbox_message(
    data: dict[str, Any],
    *,
    session: str = "gaddy-test",
    message_id: str = "M-1",
    whatsapp_timestamp: datetime = NOW,
) -> WahaInboxMessage:
    return WahaInboxMessage(
        session=session,
        message_id=message_id,
        chat_jid="61280626974807@lid",
        whatsapp_timestamp=whatsapp_timestamp,
        from_me=0,
        is_real=1,
        data=json.dumps(data),
        sha256="0" * 64,
        seq=1,
        agent_first_seen_at=NOW,
        batch_id="b-1",
    )


def _text_row(message_id: str = "M-1") -> WahaInboxMessage:
    return _inbox_message(
        {
            "Info": {
                "Chat": "61280626974807@lid",
                "IsFromMe": False,
                "IsGroup": False,
                "Type": "text",
            },
            "Message": {"conversation": "hello"},
        },
        message_id=message_id,
    )


def _history_row(message_id: str = "H-1") -> WahaInboxMessage:
    return _inbox_message(
        {
            "Info": {
                "Chat": "61280626974807@lid",
                "IsFromMe": False,
                "IsGroup": False,
                "Type": "text",
            },
            "Message": {"conversation": "synced history"},
            "SourceWebMsg": {"key": {"ID": message_id}, "status": "READ"},
        },
        message_id=message_id,
        whatsapp_timestamp=datetime(2024, 4, 22, 8, 0, 0, tzinfo=timezone.utc),
    )


class FakeRepository:
    def __init__(self, rows: list[WahaInboxMessage] | None = None) -> None:
        self.rows = list(rows or [])
        self.claimed: list[ClaimedWahaMessage] = []
        self.completed: list[ClaimedWahaMessage] = []
        self.failures: list[tuple[ClaimedWahaMessage, str]] = []
        self.complete_result = True
        self.backlog: tuple[int, int, float | None] = (0, 0, None)

    async def claim_pending(
        self, worker_id: str, *, batch_size: int, lease_seconds: int, max_attempts: int
    ) -> list[ClaimedWahaMessage]:
        claimed = [
            ClaimedWahaMessage(message=row, revision=1, worker_id=worker_id)
            for row in self.rows[:batch_size]
        ]
        self.rows = self.rows[len(claimed):]
        self.claimed.extend(claimed)
        return claimed

    async def complete_claimed(self, claimed: ClaimedWahaMessage) -> bool:
        if self.complete_result:
            self.completed.append(claimed)
            return True
        return False

    async def complete_claimed_many(
        self, claimed: Sequence[ClaimedWahaMessage]
    ) -> int:
        done = 0
        for item in claimed:
            if self.complete_result:
                self.completed.append(item)
                done += 1
        return done

    async def mark_failed(self, claimed: ClaimedWahaMessage, error: str) -> None:
        self.failures.append((claimed, error))

    async def backlog_stats(self) -> tuple[int, int, float | None]:
        return self.backlog


class FakeConnections:
    def __init__(self, connection: StoredConnection | None) -> None:
        self.connection = connection

    async def get_by_provider_id(
        self, provider: str, provider_id: str
    ) -> StoredConnection | None:
        return self.connection


class FakeIngestion:
    def __init__(self, result: bool = True) -> None:
        self.calls: list[tuple[Any, str, str]] = []
        self.result = result

    async def ingest_message(
        self, event: Any, *, user_id: str, connection_id: str
    ) -> bool:
        self.calls.append((event, user_id, connection_id))
        return self.result


def _stored_connection() -> StoredConnection:
    return StoredConnection(
        user_id="user-1",
        ref=ConnectionRef(provider="waha", provider_connection_id="gaddy-test"),
        credentials=None,  # type: ignore[arg-type]
        webhook_token_hash=b"",
        status=ConnectionStatus.CONNECTED,
        id="conn-1",
    )


def _consumer(
    repo: FakeRepository,
    connections: FakeConnections,
    ingestion: FakeIngestion,
) -> WahaMessageConsumer:
    return WahaMessageConsumer(
        repo,  # type: ignore[arg-type]
        connections,  # type: ignore[arg-type]
        ingestion,  # type: ignore[arg-type]
    )


async def test_processes_claimed_rows_end_to_end() -> None:
    repo = FakeRepository([_text_row("M-1"), _text_row("M-2")])
    connections = FakeConnections(_stored_connection())
    ingestion = FakeIngestion()
    processed = await _consumer(repo, connections, ingestion).run_once()

    assert processed == 2
    assert len(ingestion.calls) == 2
    event, user_id, connection_id = ingestion.calls[0]
    assert event.provider_message_id == "M-1"
    assert event.connection.provider == "waha"
    assert event.direction is MessageDirection.INBOUND
    assert event.text == "hello"
    assert user_id == "user-1"
    assert connection_id == "conn-1"
    assert [c.message.message_id for c in repo.completed] == ["M-1", "M-2"]
    assert repo.failures == []


async def test_history_sync_rows_are_suppressed_before_ingest() -> None:
    """SourceWebMsg-marked rows: consumed + ACKed, never ingested."""
    repo = FakeRepository([_history_row("H-1"), _history_row("H-2"), _text_row("L-1")])
    connections = FakeConnections(_stored_connection())
    ingestion = FakeIngestion()
    processed = await _consumer(repo, connections, ingestion).run_once()

    assert processed == 3
    assert {c.message.message_id for c in repo.completed} == {"H-1", "H-2", "L-1"}
    assert repo.failures == []
    # Only the live row reached ingestion; history never persisted a message.
    assert len(ingestion.calls) == 1
    assert ingestion.calls[0][0].provider_message_id == "L-1"
    assert ingestion.calls[0][0].is_history_sync is False


async def test_history_suppression_needs_no_connection() -> None:
    """Suppressed rows complete even without a mapped session connection —
    nothing resolves, schedules or enriches them."""
    repo = FakeRepository([_history_row()])
    processed = await _consumer(
        repo, FakeConnections(None), FakeIngestion()
    ).run_once()

    assert processed == 1
    assert len(repo.completed) == 1
    assert repo.failures == []


async def test_replayed_history_stays_suppressed() -> None:
    """Re-pairing redelivers the same history — suppression is idempotent."""
    for _round in range(2):
        repo = FakeRepository([_history_row("H-1"), _history_row("H-2")])
        ingestion = FakeIngestion()
        processed = await _consumer(
            repo, FakeConnections(_stored_connection()), ingestion
        ).run_once()
        assert processed == 2
        assert len(repo.completed) == 2
        assert ingestion.calls == []
        assert repo.failures == []


async def test_old_catchup_message_without_sync_marker_is_ingested() -> None:
    """Age alone never suppresses: a days-old message delivered without
    SourceWebMsg (offline catch-up) is ingested normally."""
    old_ts = datetime(2026, 11, 20, 8, 0, 0, tzinfo=timezone.utc)
    row = _inbox_message(
        {
            "Info": {
                "Chat": "61280626974807@lid",
                "IsFromMe": False,
                "IsGroup": False,
                "Type": "text",
            },
            "Message": {"conversation": "missed while offline"},
        },
        message_id="CU-1",
        whatsapp_timestamp=old_ts,
    )
    repo = FakeRepository([row])
    ingestion = FakeIngestion()
    processed = await _consumer(repo, FakeConnections(_stored_connection()), ingestion).run_once()

    assert processed == 1
    assert len(ingestion.calls) == 1
    assert len(repo.completed) == 1


async def test_large_history_flush_drains_without_ingestion() -> None:
    """A first-pairing flush drains at claim pace with zero ingest calls.

    Uses the real observed scale (~15k rows) to bound live-message delay:
    the consumer issues at most one bulk-complete per claimed batch, so a
    live row behind the flush waits ~`(backlog/batch_size)` claims, not one
    per row.
    """
    repo = FakeRepository([_history_row(f"H-{i}") for i in range(15_000)])
    repo.rows.extend([_text_row("LIVE-1")])  # live message mid-flush
    ingestion = FakeIngestion()
    consumer = WahaMessageConsumer(
        repo,  # type: ignore[arg-type]
        FakeConnections(_stored_connection()),  # type: ignore[arg-type]
        ingestion,  # type: ignore[arg-type]
        batch_size=500,
    )

    total = 0
    while processed := await consumer.run_once():
        total += processed

    assert total == 15_001
    assert len(repo.completed) == 15_001
    assert repo.failures == []
    assert [call[0].provider_message_id for call in ingestion.calls] == ["LIVE-1"]


async def test_unknown_connection_marks_row_failed() -> None:
    repo = FakeRepository([_text_row()])
    connections = FakeConnections(None)
    ingestion = FakeIngestion()
    processed = await _consumer(repo, connections, ingestion).run_once()

    assert processed == 0
    assert ingestion.calls == []
    assert repo.completed == []
    assert len(repo.failures) == 1
    assert "link the session" in repo.failures[0][1]


async def test_invalid_envelope_marks_row_failed() -> None:
    bad = _text_row()
    object.__setattr__(bad, "data", "{broken")
    repo = FakeRepository([bad])
    connections = FakeConnections(_stored_connection())
    ingestion = FakeIngestion()
    processed = await _consumer(repo, connections, ingestion).run_once()

    assert processed == 0
    assert ingestion.calls == []
    assert len(repo.failures) == 1


async def test_lost_completion_marks_row_failed() -> None:
    repo = FakeRepository([_text_row()])
    repo.complete_result = False
    connections = FakeConnections(_stored_connection())
    ingestion = FakeIngestion()
    processed = await _consumer(repo, connections, ingestion).run_once()

    assert processed == 0
    assert len(ingestion.calls) == 1  # the ingest happened, completion was fenced
    assert len(repo.failures) == 1
    assert "claim was lost" in repo.failures[0][1]


async def test_duplicate_ingestion_still_completes() -> None:
    repo = FakeRepository([_text_row()])
    connections = FakeConnections(_stored_connection())
    ingestion = FakeIngestion(result=False)  # dedup/skip
    processed = await _consumer(repo, connections, ingestion).run_once()

    assert processed == 1
    assert len(repo.completed) == 1
    assert repo.failures == []


async def test_stale_backlog_logs_warning() -> None:
    repo = FakeRepository([])
    repo.backlog = (3, 1, 7200.0)
    consumer = _consumer(repo, FakeConnections(None), FakeIngestion())
    with _capture_logs() as messages:
        await consumer._warn_on_stale_backlog(__import__("time").monotonic())
    combined = "\n".join(messages)
    assert "backlog aging" in combined
    assert "pending=3" in combined
    assert "oldest_age_seconds=7200" in combined


async def test_fresh_backlog_logs_nothing() -> None:
    repo = FakeRepository([])
    repo.backlog = (5, 0, 120.0)
    consumer = _consumer(repo, FakeConnections(None), FakeIngestion())
    with _capture_logs() as messages:
        await consumer._warn_on_stale_backlog(__import__("time").monotonic())
    assert not any("backlog aging" in m for m in messages)


async def test_backlog_warning_is_throttled() -> None:
    import time

    repo = FakeRepository([])
    repo.backlog = (3, 0, 7200.0)
    consumer = _consumer(repo, FakeConnections(None), FakeIngestion())
    with _capture_logs() as messages:
        await consumer._warn_on_stale_backlog(time.monotonic())
        await consumer._warn_on_stale_backlog(time.monotonic())
    assert sum("backlog aging" in m for m in messages) == 1
