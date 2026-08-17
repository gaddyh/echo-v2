"""Postgres Unit of Work tests (requires Docker).

The canonical scenario: claim a webhook event AND update the connection
status in one transaction. If either fails, both roll back — a crash after
claim but before status update must not permanently lose the state
transition (the provider retry can re-claim because the claim rolled back).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from echo_v2.ports.whatsapp import ConnectionRef, ConnectionStatus
from tests.persistence.conftest import insert_user

pytestmark = pytest.mark.asyncio


async def _seed_connection(session_factory, user_id: str, provider_id: str = "111") -> str:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "INSERT INTO whatsapp_connections "
                "(user_id, provider, provider_connection_id, credentials, "
                " webhook_token_hash, connection_status) "
                "VALUES (:uid, 'green', :pid, :creds, :wh, 'connecting') "
                "RETURNING id"
            ),
            {"uid": user_id, "pid": provider_id, "creds": b"enc", "wh": b"hash"},
        )
        conn_id = str(result.scalar_one())
        await session.commit()
    return conn_id


async def test_uow_claims_webhook_and_updates_status_atomically(
    unit_of_work_factory, session_factory
):
    """The happy path: claim + update_status in one commit."""
    user_id = await insert_user(session_factory)
    conn_id = await _seed_connection(session_factory, user_id)

    async with unit_of_work_factory() as uow:
        claimed = await uow.webhooks.claim(
            "evt-uow-1", provider="green", connection_id=conn_id, event_type="state"
        )
        assert claimed is True
        await uow.connections.update_status(
            ConnectionRef("green", "111"),
            ConnectionStatus.CONNECTED,
            "authorized",
        )

    # Both committed.
    async with session_factory() as session:
        evt = (await session.execute(
            text("SELECT event_id FROM provider_webhook_events WHERE event_id='evt-uow-1'")
        )).scalar_one_or_none()
        assert evt == "evt-uow-1"
        status = (await session.execute(
            text("SELECT connection_status FROM whatsapp_connections WHERE id=:cid"),
            {"cid": conn_id},
        )).scalar_one()
        assert status == "connected"


async def test_uow_rollback_on_exception(unit_of_work_factory, session_factory):
    """If update_status raises, the claim must roll back too."""
    user_id = await insert_user(session_factory)
    conn_id = await _seed_connection(session_factory, user_id)

    with pytest.raises(RuntimeError):
        async with unit_of_work_factory() as uow:
            claimed = await uow.webhooks.claim(
                "evt-uow-2", provider="green", connection_id=conn_id
            )
            assert claimed is True
            raise RuntimeError("simulated failure after claim")

    # The claim was rolled back — the event row must not exist.
    async with session_factory() as session:
        evt = (await session.execute(
            text("SELECT event_id FROM provider_webhook_events WHERE event_id='evt-uow-2'")
        )).scalar_one_or_none()
        assert evt is None


async def test_uow_repos_share_one_session(unit_of_work_factory):
    """All three repos in a UoW use the same AsyncSession (no independent commits)."""
    async with unit_of_work_factory() as uow:
        # The shared session is the same object across repos.
        assert uow.connections._shared_session is uow.webhooks._shared_session
        assert uow.webhooks._shared_session is uow.idempotency._shared_session


async def test_uow_claim_duplicate_inside_transaction(unit_of_work_factory, session_factory):
    """A second claim for the same event inside a UoW returns False (duplicate)."""
    user_id = await insert_user(session_factory)
    conn_id = await _seed_connection(session_factory, user_id)

    async with unit_of_work_factory() as uow:
        first = await uow.webhooks.claim(
            "evt-uow-3", provider="green", connection_id=conn_id
        )
        second = await uow.webhooks.claim(
            "evt-uow-3", provider="green", connection_id=conn_id
        )
        assert first is True
        assert second is False
