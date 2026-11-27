"""Postgres-backed integration tests for the WAHA event inbox.

Starts a Postgres Testcontainer, runs migrations, and verifies the batch
upsert contract: inserts, duplicates (no state reset), updates (revision
bump + status reset), same-id-different-session isolation, concurrency
(duplicate delivery, competing updates), transaction rollback, lost-ACK
resend, the revision-guarded processing mark, and the accepted Phase 1
last-writer-wins stale-retry behavior.

Requires Docker. Skips locally if Docker is unavailable; fails in CI.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.waha import WahaInboxMessage
from echo_v2.persistence.orm import WahaEventInboxRow
from echo_v2.persistence.postgres_waha_event_inbox import (
    PostgresWahaEventInboxRepository,
)

NOW = datetime(2026, 11, 26, 17, 59, 1, tzinfo=timezone.utc)
FIRST_SEEN = datetime(2026, 11, 26, 17, 58, 51, tzinfo=timezone.utc)
RAW = '{ "info": { "text": "שלום world" },  "n": 1 }'


def _message(
    *,
    session: str = "gaddy-test",
    message_id: str = "AC3B05AF332C19A84A8C9EF9C0B1FDD3",
    data: str = RAW,
    chat_jid: str = "972544998652-1584868277@g.us",
    from_me: int = 0,
    is_real: int = 0,
    seq: int = 7,
    batch_id: str = "1795718136-14",
    sha256: str | None = None,
) -> WahaInboxMessage:
    return WahaInboxMessage(
        session=session,
        message_id=message_id,
        chat_jid=chat_jid,
        whatsapp_timestamp=NOW,
        from_me=from_me,
        is_real=is_real,
        data=data,
        sha256=sha256 or hashlib.sha256(data.encode("utf-8")).hexdigest(),
        seq=seq,
        agent_first_seen_at=FIRST_SEEN,
        batch_id=batch_id,
    )


def _updated(message: WahaInboxMessage, *, data: str, **overrides) -> WahaInboxMessage:
    return WahaInboxMessage(
        session=overrides.get("session", message.session),
        message_id=overrides.get("message_id", message.message_id),
        chat_jid=overrides.get("chat_jid", message.chat_jid),
        whatsapp_timestamp=overrides.get("whatsapp_timestamp", message.whatsapp_timestamp),
        from_me=overrides.get("from_me", message.from_me),
        is_real=overrides.get("is_real", message.is_real),
        data=data,
        sha256=hashlib.sha256(data.encode("utf-8")).hexdigest(),
        seq=overrides.get("seq", message.seq),
        agent_first_seen_at=overrides.get(
            "agent_first_seen_at", message.agent_first_seen_at
        ),
        batch_id=overrides.get("batch_id", message.batch_id),
    )


@pytest_asyncio.fixture
async def repo(
    session_factory: async_sessionmaker[AsyncSession], clean_db: None
) -> PostgresWahaEventInboxRepository:
    return PostgresWahaEventInboxRepository(session_factory)


async def _fetch_row(
    session_factory: async_sessionmaker[AsyncSession],
    session: str,
    message_id: str,
) -> WahaEventInboxRow:
    async with session_factory() as db:
        return (
            (
                await db.execute(
                    select(WahaEventInboxRow).where(
                        WahaEventInboxRow.session == session,
                        WahaEventInboxRow.message_id == message_id,
                    )
                )
            )
            .scalars()
            .one()
        )


async def _count(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as db:
        return (
            await db.execute(select(func.count()).select_from(WahaEventInboxRow))
        ).scalar_one()


async def test_insert_new_message_pending_revision_one(
    repo: PostgresWahaEventInboxRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    message = _message()
    result = await repo.ingest_batch("b-1", [message])

    assert result.inserted == 1
    assert result.updated == 0
    assert result.duplicated == 0
    stored = await repo.get(message.session, message.message_id)
    assert stored is not None
    # Exact raw data string preserved — no reserialization.
    assert stored.data == RAW
    assert stored.from_me == 0
    assert stored.is_real == 0
    assert stored.chat_jid == message.chat_jid

    row = await _fetch_row(session_factory, message.session, message.message_id)
    assert row.status == "pending"
    assert row.revision == 1
    assert row.batch_id == "b-1"


async def test_identical_resend_is_duplicate_without_state_reset(
    repo: PostgresWahaEventInboxRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    message = _message()
    await repo.ingest_batch("b-1", [message])
    # Simulate downstream processing marking it done.
    assert await repo.mark_processed(message.session, message.message_id, revision=1)

    # Lost ACK → agent resends the identical message in a new batch.
    result = await repo.ingest_batch("b-2", [_message(batch_id="b-2")])

    assert result.inserted == 0
    assert result.updated == 0
    assert result.duplicated == 1
    row = await _fetch_row(session_factory, message.session, message.message_id)
    assert row.status == "processed"  # processing state NOT reset
    assert row.revision == 1
    assert row.batch_id == "b-1"  # original row untouched
    assert await _count(session_factory) == 1


async def test_different_hash_updates_payload_resets_status(
    repo: PostgresWahaEventInboxRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    message = _message()
    await repo.ingest_batch("b-1", [message])
    assert await repo.mark_processed(message.session, message.message_id, revision=1)

    revised = _updated(message, data='{"edit": "new content"}', batch_id="b-2", seq=8)
    result = await repo.ingest_batch("b-2", [revised])

    assert result.inserted == 0
    assert result.updated == 1
    assert result.duplicated == 0

    row = await _fetch_row(session_factory, message.session, message.message_id)
    assert row.status == "pending"  # new revision requires processing
    assert row.revision == 2
    assert row.data == '{"edit": "new content"}'
    assert row.sha256 == revised.sha256
    assert row.seq == 8
    assert row.batch_id == "b-2"
    assert row.last_updated_at >= row.first_received_at


async def test_same_message_id_in_different_sessions(
    repo: PostgresWahaEventInboxRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    a = _message(session="session-a")
    b = _message(session="session-b")
    result = await repo.ingest_batch("b-1", [a, b])

    assert result.inserted == 2
    assert await _count(session_factory) == 2
    assert (await repo.get("session-a", a.message_id)) is not None
    assert (await repo.get("session-b", b.message_id)) is not None


async def test_empty_batch_is_a_no_op(
    repo: PostgresWahaEventInboxRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result = await repo.ingest_batch("b-empty", [])
    assert (result.inserted, result.updated, result.duplicated) == (0, 0, 0)
    assert await _count(session_factory) == 0


async def test_concurrent_duplicate_delivery(
    repo: PostgresWahaEventInboxRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two identical batches delivered concurrently (e.g. retried request)."""
    message = _message()
    results = await asyncio.gather(
        repo.ingest_batch("b-1", [message]),
        repo.ingest_batch("b-1", [_message()]),
    )

    inserted = sum(r.inserted for r in results)
    duplicated = sum(r.duplicated for r in results)
    assert inserted == 1
    assert duplicated == 1
    assert await _count(session_factory) == 1


async def test_concurrent_updates(
    repo: PostgresWahaEventInboxRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two different revisions delivered concurrently: one insert, one
    update, no deadlock, final revision exactly 2."""
    base = _message()
    v2 = _updated(base, data='{"v": 2}', batch_id="b-2")
    v3 = _updated(base, data='{"v": 3}', batch_id="b-3")

    results = await asyncio.gather(
        repo.ingest_batch("b-2", [v2]),
        repo.ingest_batch("b-3", [v3]),
    )

    inserted = sum(r.inserted for r in results)
    updated = sum(r.updated for r in results)
    assert inserted == 1
    assert updated == 1
    row = await _fetch_row(session_factory, base.session, base.message_id)
    assert row.revision == 2
    assert row.data in ('{"v": 2}', '{"v": 3}')
    assert row.status == "pending"


async def test_transaction_rollback_persists_nothing(
    repo: PostgresWahaEventInboxRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A failure anywhere in the batch rolls the whole batch back."""
    valid = _message(message_id="VALID-1")
    # from_me=7 violates the CHECK constraint — rejected by the database.
    invalid = _message(message_id="INVALID-1", from_me=7)

    with pytest.raises(IntegrityError):
        await repo.ingest_batch("b-bad", [valid, invalid])

    assert await _count(session_factory) == 0
    assert await repo.get(valid.session, valid.message_id) is None


async def test_mark_processed_revision_guard(
    repo: PostgresWahaEventInboxRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = _message()
    await repo.ingest_batch("b-1", [base])
    revised = _updated(base, data='{"edit": true}', batch_id="b-2")
    await repo.ingest_batch("b-2", [revised])

    # A processor that only handled revision 1 must not mark revision 2
    # processed.
    assert await repo.mark_processed(base.session, base.message_id, revision=1) is False
    row = await _fetch_row(session_factory, base.session, base.message_id)
    assert row.status == "pending"

    assert await repo.mark_processed(base.session, base.message_id, revision=2) is True
    row = await _fetch_row(session_factory, base.session, base.message_id)
    assert row.status == "processed"


async def test_stale_retry_overwrites_newer_revision_last_writer_wins(
    repo: PostgresWahaEventInboxRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Accepted Phase 1 limitation: without a globally monotonic revision
    ID, a stale retry of the OLD payload overwrites the newer revision
    (last-writer-wins) and resets processing. Documented in AGENTS.md; must
    be revisited before enabling downstream processing."""
    base = _message(data='{"v": 1}')
    await repo.ingest_batch("b-1", [base])
    newer = _updated(base, data='{"v": 2}', batch_id="b-2")
    await repo.ingest_batch("b-2", [newer])

    # The agent retries batch b-1 after a network error — its payload is
    # older than what is stored.
    stale_result = await repo.ingest_batch("b-1", [_message(data='{"v": 1}')])

    assert stale_result.inserted == 0
    assert stale_result.updated == 1  # stale payload overwrote the newer one
    row = await _fetch_row(session_factory, base.session, base.message_id)
    assert row.revision == 3
    assert row.data == '{"v": 1}'
    assert row.status == "pending"


# --- claim lifecycle (Phase 2 consumer) ---------------------------------------


async def test_claim_pending_oldest_first_with_lease(
    repo: PostgresWahaEventInboxRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    a = _message(message_id="A-1")
    b = _message(message_id="B-1")
    await repo.ingest_batch("b-1", [a])
    await repo.ingest_batch("b-2", [b])

    claimed = await repo.claim_pending(
        "worker-1", batch_size=10, lease_seconds=60, max_attempts=5
    )
    assert [c.message.message_id for c in claimed] == ["A-1", "B-1"]
    for item in claimed:
        assert item.worker_id == "worker-1"
        assert item.revision == 1
    row = await _fetch_row(session_factory, a.session, "A-1")
    assert row.status == "processing"
    assert row.lease_owner == "worker-1"
    assert row.lease_expires_at is not None
    assert row.attempt_count == 1

    # Claimed rows are not re-claimed while the lease is fresh.
    assert await repo.claim_pending("worker-2", batch_size=10, lease_seconds=60) == []


async def test_complete_claimed_marks_processed_once(
    repo: PostgresWahaEventInboxRepository,
) -> None:
    message = _message()
    await repo.ingest_batch("b-1", [message])
    claimed = (
        await repo.claim_pending("worker-1", batch_size=1, lease_seconds=60)
    )[0]

    assert await repo.complete_claimed(claimed) is True
    # Second completion is a no-op (already processed).
    assert await repo.complete_claimed(claimed) is False


async def test_mark_failed_releases_row_for_retry(
    repo: PostgresWahaEventInboxRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    message = _message()
    await repo.ingest_batch("b-1", [message])
    claimed = (
        await repo.claim_pending("worker-1", batch_size=1, lease_seconds=60)
    )[0]

    await repo.mark_failed(claimed, "boom")

    row = await _fetch_row(session_factory, message.session, message.message_id)
    assert row.status == "failed"
    assert row.lease_owner is None
    assert row.attempt_count == 1
    assert row.last_error == "boom"

    # Re-claimed on the next poll, attempt_count grows.
    reclaimed = await repo.claim_pending(
        "worker-2", batch_size=1, lease_seconds=60
    )
    assert len(reclaimed) == 1
    assert reclaimed[0].worker_id == "worker-2"
    row = await _fetch_row(session_factory, message.session, message.message_id)
    assert row.attempt_count == 2


async def test_attempt_cap_stops_retrying(
    repo: PostgresWahaEventInboxRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    message = _message()
    await repo.ingest_batch("b-1", [message])
    claimed = (
        await repo.claim_pending("worker-1", batch_size=1, lease_seconds=60, max_attempts=1)
    )[0]
    await repo.mark_failed(claimed, "terminal-ish")

    assert (
        await repo.claim_pending("worker-1", batch_size=1, lease_seconds=60, max_attempts=1)
        == []
    )
    row = await _fetch_row(session_factory, message.session, message.message_id)
    assert row.status == "failed"


async def test_expired_lease_is_reclaimed(
    repo: PostgresWahaEventInboxRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    message = _message()
    await repo.ingest_batch("b-1", [message])
    await repo.claim_pending("worker-1", batch_size=1, lease_seconds=60)
    # Simulate a crashed worker: lease expired in place.
    async with session_factory() as db:
        await db.execute(
            text(
                "UPDATE waha_event_inbox SET lease_expires_at = "
                "now() - interval '120 seconds' WHERE session = :s AND message_id = :m"
            ),
            {"s": message.session, "m": message.message_id},
        )
        await db.commit()

    reclaimed = await repo.claim_pending("worker-2", batch_size=1, lease_seconds=60)
    assert len(reclaimed) == 1
    assert reclaimed[0].worker_id == "worker-2"


async def test_complete_claimed_rejected_when_revision_advanced(
    repo: PostgresWahaEventInboxRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = _message()
    await repo.ingest_batch("b-1", [base])
    claimed = (
        await repo.claim_pending("worker-1", batch_size=1, lease_seconds=60)
    )[0]

    # The payload is updated mid-processing: revision bumps, status resets.
    revised = _updated(base, data='{"edit": true}', batch_id="b-2")
    await repo.ingest_batch("b-2", [revised])

    # The stale processor cannot complete its claim.
    assert await repo.complete_claimed(claimed) is False
    row = await _fetch_row(session_factory, base.session, base.message_id)
    assert row.status == "pending"
    assert row.revision == 2

    # The new revision is claimable and completable.
    reclaimed = (
        await repo.claim_pending("worker-2", batch_size=1, lease_seconds=60)
    )[0]
    assert reclaimed.revision == 2
    assert await repo.complete_claimed(reclaimed) is True


async def test_backlog_stats_counts_and_age(
    repo: PostgresWahaEventInboxRepository,
) -> None:
    assert await repo.backlog_stats() == (0, 0, None)

    a = _message(message_id="A-1")
    b = _message(message_id="B-1")
    await repo.ingest_batch("b-1", [a])
    claimed = (await repo.claim_pending("w", batch_size=1, lease_seconds=60))[0]
    await repo.mark_failed(claimed, "boom")
    await repo.ingest_batch("b-2", [b])

    pending, failed, oldest_age = await repo.backlog_stats()
    assert pending == 1
    assert failed == 1
    assert oldest_age is not None and 0 <= oldest_age < 60


async def test_claim_and_completion_set_latency_timestamps(
    repo: PostgresWahaEventInboxRepository,
) -> None:
    """Operations Center latency stages: claimed_at / processed_at are set."""
    from sqlalchemy import text

    message = _message()
    await repo.ingest_batch("b-1", [message])
    claimed = (
        await repo.claim_pending("worker-1", batch_size=1, lease_seconds=60)
    )[0]
    async with repo._session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT claimed_at, processed_at FROM waha_event_inbox"
                    " WHERE session = :s AND message_id = :m"
                ),
                {"s": message.session, "m": message.message_id},
            )
        ).one()
    assert row.claimed_at is not None
    assert row.processed_at is None

    assert await repo.complete_claimed(claimed) is True
    async with repo._session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT claimed_at, processed_at FROM waha_event_inbox"
                    " WHERE session = :s AND message_id = :m"
                ),
                {"s": message.session, "m": message.message_id},
            )
        ).one()
    assert row.processed_at is not None
    assert row.processed_at >= row.claimed_at


async def test_revision_update_resets_latency_timestamps(
    repo: PostgresWahaEventInboxRepository,
) -> None:
    """A new payload revision resets claimed_at/processed_at with status."""
    from sqlalchemy import text

    message = _message()
    await repo.ingest_batch("b-1", [message])
    claimed = (
        await repo.claim_pending("worker-1", batch_size=1, lease_seconds=60)
    )[0]
    assert await repo.complete_claimed(claimed) is True

    revised = replace(message, data='{"text": "v2"}', sha256=None)
    revised = replace(
        revised,
        sha256=hashlib.sha256(revised.data.encode("utf-8")).hexdigest(),
    )
    await repo.ingest_batch("b-2", [revised])
    async with repo._session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, claimed_at, processed_at FROM waha_event_inbox"
                    " WHERE session = :s AND message_id = :m"
                ),
                {"s": message.session, "m": message.message_id},
            )
        ).one()
    assert row.status == "pending"
    assert row.claimed_at is None
    assert row.processed_at is None
