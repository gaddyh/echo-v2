"""Tests for PostgresWebhookInbox — persistent inbox lifecycle."""

from __future__ import annotations

from datetime import timedelta

import pytest

from echo_v2.app.webhooks.inbox import PostgresWebhookInbox


@pytest.mark.asyncio
async def test_claim_new_event_returns_true(bot_inbox: PostgresWebhookInbox):
    assert await bot_inbox.claim("wamid.NEW1") is True


@pytest.mark.asyncio
async def test_claim_processed_event_returns_false(bot_inbox: PostgresWebhookInbox):
    await bot_inbox.claim("wamid.PROC1")
    await bot_inbox.succeed("wamid.PROC1")
    assert await bot_inbox.claim("wamid.PROC1") is False


@pytest.mark.asyncio
async def test_claim_processing_event_returns_false(bot_inbox: PostgresWebhookInbox):
    await bot_inbox.claim("wamid.PROC2")
    # Still processing — not re-claimable.
    assert await bot_inbox.claim("wamid.PROC2") is False


@pytest.mark.asyncio
async def test_failed_event_is_reclaimable(bot_inbox: PostgresWebhookInbox):
    await bot_inbox.claim("wamid.FAIL1")
    await bot_inbox.fail("wamid.FAIL1", "dispatch error")
    # Failed events can be re-claimed on retry.
    assert await bot_inbox.claim("wamid.FAIL1") is True


@pytest.mark.asyncio
async def test_succeed_marks_processed(bot_inbox: PostgresWebhookInbox):
    await bot_inbox.claim("wamid.S1")
    await bot_inbox.succeed("wamid.S1")
    assert await bot_inbox.claim("wamid.S1") is False


@pytest.mark.asyncio
async def test_fail_records_error(bot_inbox: PostgresWebhookInbox, session_factory):
    from sqlalchemy import text

    await bot_inbox.claim("wamid.F1")
    await bot_inbox.fail("wamid.F1", "boom")
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT status, error FROM bot_webhook_events WHERE event_id = 'wamid.F1'")
            )
        ).one()
    assert row[0] == "failed"
    assert row[1] == "boom"


@pytest.mark.asyncio
async def test_reclaim_stale_resets_processing(bot_inbox: PostgresWebhookInbox, session_factory):
    from sqlalchemy import text

    await bot_inbox.claim("wamid.STALE1")
    # Manually backdate updated_at so it's older than the lease timeout.
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE bot_webhook_events SET updated_at = now() - interval '10 minutes' "
                "WHERE event_id = 'wamid.STALE1'"
            )
        )
        await session.commit()

    reclaimed = await bot_inbox.reclaim_stale(timedelta(minutes=5))
    assert reclaimed == 1

    # Now the stale processing entry is failed and re-claimable.
    assert await bot_inbox.claim("wamid.STALE1") is True


@pytest.mark.asyncio
async def test_reclaim_stale_leaves_recent_processing(bot_inbox: PostgresWebhookInbox):
    await bot_inbox.claim("wamid.RECENT1")
    reclaimed = await bot_inbox.reclaim_stale(timedelta(minutes=5))
    assert reclaimed == 0
    # Still processing — not re-claimable.
    assert await bot_inbox.claim("wamid.RECENT1") is False


@pytest.mark.asyncio
async def test_full_lifecycle_claim_process_succeed(bot_inbox: PostgresWebhookInbox):
    """End-to-end: claim → succeed → duplicate."""
    assert await bot_inbox.claim("wamid.LIFE1") is True
    await bot_inbox.succeed("wamid.LIFE1")
    assert await bot_inbox.claim("wamid.LIFE1") is False


@pytest.mark.asyncio
async def test_full_lifecycle_claim_fail_reclaim_succeed(bot_inbox: PostgresWebhookInbox):
    """End-to-end: claim → fail → re-claim → succeed."""
    assert await bot_inbox.claim("wamid.LIFE2") is True
    await bot_inbox.fail("wamid.LIFE2", "first attempt failed")
    assert await bot_inbox.claim("wamid.LIFE2") is True
    await bot_inbox.succeed("wamid.LIFE2")
    assert await bot_inbox.claim("wamid.LIFE2") is False
