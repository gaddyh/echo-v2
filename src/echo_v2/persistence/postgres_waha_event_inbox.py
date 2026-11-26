"""PostgreSQL repository for the WAHA event inbox.

``ingest_batch`` persists an entire batch with one statement inside one
transaction (all-or-nothing) using PostgreSQL's ``INSERT ... ON CONFLICT DO
UPDATE ... RETURNING``:

* ``ON CONFLICT (session, message_id) DO UPDATE ... WHERE sha256 <> EXCLUDED.sha256``
  — a conflicting row is only rewritten when the payload hash changed; an
  identical retry (same hash) matches no row and leaves all state intact.
* ``RETURNING (xmax = 0)`` distinguishes inserted rows from updated ones.
* Rows are sorted by ``(session, message_id)`` so concurrent batches touch
  rows in a consistent order — no deadlocks between overlapping batches.
* ``SET LOCAL synchronous_commit = on`` makes the durable-commit assumption
  explicit: ``commit()`` returns only after the WAL flush, which is exactly
  when the caller may ACK.

The transaction commits before ``ingest_batch`` returns; if it raises, the
batch is rolled back and nothing was persisted.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import CursorResult, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.waha import WahaBatchIngestResult, WahaInboxMessage
from echo_v2.persistence.orm import WahaEventInboxRow

__all__ = ["PostgresWahaEventInboxRepository"]


def _to_domain(row: WahaEventInboxRow) -> WahaInboxMessage:
    return WahaInboxMessage(
        session=row.session,
        message_id=row.message_id,
        chat_jid=row.chat_jid,
        whatsapp_timestamp=row.whatsapp_timestamp,
        from_me=row.from_me,
        is_real=row.is_real,
        data=row.data,
        sha256=row.sha256,
        seq=row.seq,
        agent_first_seen_at=row.agent_first_seen_at,
        batch_id=row.batch_id,
    )


class PostgresWahaEventInboxRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def ingest_batch(
        self, batch_id: str, messages: Sequence[WahaInboxMessage]
    ) -> WahaBatchIngestResult:
        if not messages:
            return WahaBatchIngestResult(inserted=0, updated=0, duplicated=0)

        # Consistent row-lock order across concurrent batches.
        rows = sorted(messages, key=lambda m: (m.session, m.message_id))
        now = datetime.now(timezone.utc)

        values_sql: list[str] = []
        params: dict[str, Any] = {"now": now}
        for i, m in enumerate(rows):
            values_sql.append(
                f"(:session_{i}, :message_id_{i}, :chat_jid_{i}, "
                f":whatsapp_timestamp_{i}, :from_me_{i}, :is_real_{i}, "
                f":data_{i}, :sha256_{i}, :seq_{i}, :agent_first_seen_at_{i}, "
                f":batch_id, :now, :now, 'pending', 1)"
            )
            params.update(
                {
                    f"session_{i}": m.session,
                    f"message_id_{i}": m.message_id,
                    f"chat_jid_{i}": m.chat_jid,
                    f"whatsapp_timestamp_{i}": m.whatsapp_timestamp,
                    f"from_me_{i}": m.from_me,
                    f"is_real_{i}": m.is_real,
                    f"data_{i}": m.data,
                    f"sha256_{i}": m.sha256,
                    f"seq_{i}": m.seq,
                    f"agent_first_seen_at_{i}": m.agent_first_seen_at,
                }
            )
        params["batch_id"] = batch_id

        sql = (
            "INSERT INTO waha_event_inbox ("
            " session, message_id, chat_jid, whatsapp_timestamp, from_me, is_real,"
            " data, sha256, seq, agent_first_seen_at, batch_id,"
            " first_received_at, last_updated_at, status, revision)"
            f" VALUES {', '.join(values_sql)}"
            " ON CONFLICT (session, message_id) DO UPDATE SET"
            "  chat_jid = EXCLUDED.chat_jid,"
            "  whatsapp_timestamp = EXCLUDED.whatsapp_timestamp,"
            "  from_me = EXCLUDED.from_me,"
            "  is_real = EXCLUDED.is_real,"
            "  data = EXCLUDED.data,"
            "  sha256 = EXCLUDED.sha256,"
            "  seq = EXCLUDED.seq,"
            "  agent_first_seen_at = EXCLUDED.agent_first_seen_at,"
            "  batch_id = EXCLUDED.batch_id,"
            "  last_updated_at = :now,"
            "  status = 'pending',"
            "  revision = waha_event_inbox.revision + 1"
            " WHERE waha_event_inbox.sha256 <> EXCLUDED.sha256"
            " RETURNING session, message_id, sha256, (xmax = 0) AS inserted"
        )

        # One transaction for the whole batch: `session.begin()` commits on
        # clean exit and rolls back on any error. Nothing is acknowledged
        # until the commit has returned.
        async with self._session_factory() as session, session.begin():
            await session.execute(text("SET LOCAL synchronous_commit = on"))
            result = await session.execute(text(sql), params)
            returned = result.mappings().all()

        inserted = sum(1 for row in returned if row["inserted"])
        updated = len(returned) - inserted
        duplicated = len(rows) - len(returned)
        return WahaBatchIngestResult(
            inserted=inserted, updated=updated, duplicated=duplicated
        )

    async def get(self, session: str, message_id: str) -> WahaInboxMessage | None:
        async with self._session_factory() as db_session:
            row = (
                (
                    await db_session.execute(
                        select(WahaEventInboxRow).where(
                            WahaEventInboxRow.session == session,
                            WahaEventInboxRow.message_id == message_id,
                        )
                    )
                )
                .scalars()
                .one_or_none()
            )
            return _to_domain(row) if row is not None else None

    async def mark_processed(
        self, session: str, message_id: str, *, revision: int
    ) -> bool:
        async with self._session_factory() as db_session:
            result = cast(
                CursorResult[Any],
                await db_session.execute(
                    text(
                        """
                        UPDATE waha_event_inbox
                           SET status = 'processed', last_updated_at = now()
                         WHERE session = :session AND message_id = :message_id
                           AND revision = :revision
                           AND status IN ('pending', 'processing')
                        """
                    ),
                    {"session": session, "message_id": message_id, "revision": revision},
                ),
            )
            await db_session.commit()
            return result.rowcount == 1
