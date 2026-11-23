"""Backfill event_inbox.processed_at for events Echo already consumed.

Usage:
    .venv/bin/python scripts/backfill_event_inbox_processed.py            # dry run
    .venv/bin/python scripts/backfill_event_inbox_processed.py --yes      # execute

Historical context: until the fix marking connection_state events consumed
in ``BaileysEventRepository.process_claimed_event``, only message events set
``whatsapp_connector.event_inbox.processed_at``. State events were marked
only in Echo's ledger (``baileys_connector_event_processing``), leaving
their transport rows "unprocessed" until the connector's 30-day
never-consumed retention valve deleted them — and every claim poll scanned
them.

This script marks transport rows processed for exactly the events whose
ledger row says ``processed``, using the historical ledger timestamp, in
batches to keep transactions short. It is idempotent (guarded by
``processed_at IS NULL``) and safe to re-run after an interruption.

Side effect: once rows carry ``processed_at``, the connector's hourly
retention sweep deletes consumed rows older than EVENT_RETENTION_DAYS
(default 7). That is the intended outcome — the rows are already-consumed
transport data.

The Echo DATABASE_URL is loaded from ``echo-v2/.env``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_ROOT = Path(__file__).resolve().parent.parent

_COUNT_SQL = text(
    """
    SELECT e.event_type, count(*) AS n
      FROM whatsapp_connector.event_inbox e
      JOIN baileys_connector_event_processing p ON p.inbox_id = e.id
     WHERE e.processed_at IS NULL AND p.status = 'processed'
     GROUP BY e.event_type
     ORDER BY e.event_type
    """
)

_BATCH_SQL = text(
    """
    WITH candidates AS (
        SELECT e.id
          FROM whatsapp_connector.event_inbox e
          JOIN baileys_connector_event_processing p ON p.inbox_id = e.id
         WHERE e.processed_at IS NULL AND p.status = 'processed'
         ORDER BY e.id
         LIMIT :batch_size
    )
    UPDATE whatsapp_connector.event_inbox e
       SET processed_at = COALESCE(p.processed_at, p.updated_at)
      FROM baileys_connector_event_processing p, candidates c
     WHERE e.id = c.id AND p.inbox_id = e.id AND p.status = 'processed'
    """
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Execute the backfill after showing the dry-run report",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5000,
        help="Rows per batch transaction (default: 5000)",
    )
    return parser


def _async_database_url(url: str) -> str:
    """Use the installed psycopg3 driver even if .env uses bare PostgreSQL URL."""
    parts = urlsplit(url)
    if parts.scheme == "postgresql":
        return urlunsplit(
            ("postgresql+psycopg", parts.netloc, parts.path, parts.query, parts.fragment)
        )
    return url


async def _report(engine: AsyncEngine) -> list[tuple[str, int]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(_COUNT_SQL)).all()
    return [(str(row.event_type), int(row.n)) for row in rows]


async def main() -> None:
    args = _parser().parse_args()
    load_dotenv(_ROOT / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is missing from .env")
    engine = create_async_engine(_async_database_url(database_url))
    try:
        pending = await _report(engine)
        total = sum(n for _, n in pending)
        if not pending:
            print("Nothing to backfill: no event_inbox rows awaiting a processed ledger mark.")
            return
        print("event_inbox rows to mark processed (dry-run report):")
        for event_type, n in pending:
            print(f"  {event_type}: {n}")
        print(f"Total: {total}")
        if not args.yes:
            print("Dry run only — re-run with --yes to apply.")
            return

        marked = 0
        while True:
            async with engine.begin() as conn:
                updated = int((await conn.execute(_BATCH_SQL, {"batch_size": args.batch_size})).rowcount)
            marked += updated
            print(f"  marked {marked}/{total}")
            if updated < args.batch_size:
                break
        remaining = sum(n for _, n in await _report(engine))
        print(f"Done: marked {marked} rows, {remaining} remaining (expected 0).")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
