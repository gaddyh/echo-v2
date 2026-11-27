"""Re-drive WAHA child-phone verification for an already-connected child.

Usage:
    .venv/bin/python scripts/waha_reverify_child.py --phone +972512217037

Recovery for a child whose WAHA session connected before the agent
reported the account identity (verification pending). The script:

1. Resolves the child's WAHA session from the persisted state.
2. Queues a ``get_status`` command through the normal lease-bounded
   command queue (``WahaSessionService.reconcile_session`` — the existing
   recovery primitive; never a direct WAHA HTTP call).
3. Prints the current verification-relevant state.

The agent executes the command, reads the account identity from the local
WAHA session API and reports it as a ``command_result`` event. Echo
persists the identity atomically with the lifecycle transition, which
re-fires the ``confirm_working`` effect and re-runs child-phone
verification automatically. Guard activates only if the normalized
provider-reported phone matches the child's phone.

The command round-trip is asynchronous (agent poll interval ~2s plus
network). Re-run with ``--check`` after a few seconds to inspect the
result, or watch the Operations Center management section.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from echo_v2.integrations.waha.settings import load_waha_settings
from echo_v2.persistence.postgres_waha_sessions import (
    PostgresWahaSessionRepository,
)
from echo_v2.services.waha_session_service import WahaSessionService


def _database_url() -> str:
    load_dotenv(_ROOT / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


async def _state(session_factory, phone: str) -> None:
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT u.id AS user_id, u.onboarding_status,
                           c.connection_status, c.provider_raw_status,
                           s.session_name, s.worker_id, s.status AS session_status,
                           s.raw_status, s.account_phone, s.account_identity_at,
                           (SELECT count(*) FROM guardian_child_links g
                             WHERE g.child_user_id = u.id AND g.status = 'active')
                               AS active_guardian_links,
                           (SELECT count(*) FROM guardian_child_links g
                             WHERE g.child_user_id = u.id AND g.status = 'pending')
                               AS pending_guardian_links,
                           (SELECT count(*) FROM waha_commands wc
                             WHERE wc.session_name = s.session_name
                               AND wc.status IN ('pending', 'claimed'))
                               AS outstanding_commands
                      FROM users u
                      JOIN whatsapp_connections c ON c.user_id = u.id
                      LEFT JOIN waha_sessions s ON s.user_id = u.id
                     WHERE u.phone_number = :phone
                    """
                ),
                {"phone": phone},
            )
        ).mappings().all()
    if not rows:
        print(f"no user+connection found for {phone}")
        return
    for row in rows:
        print("state:")
        print(f"  user_id:               {row['user_id']}")
        print(f"  onboarding_status:     {row['onboarding_status']}")
        print(f"  connection:            {row['connection_status']} ({row['provider_raw_status']})")
        print(f"  session:               {row['session_name']} on {row['worker_id']}")
        print(f"  session status:        {row['session_status']} ({row['raw_status']})")
        print(f"  account identity:      {'present' if row['account_phone'] else 'MISSING'} (at {row['account_identity_at']})")
        print(f"  guardian links:        {row['active_guardian_links']} active / {row['pending_guardian_links']} pending")
        print(f"  outstanding commands:  {row['outstanding_commands']}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone", required=True, help="child phone, E.164")
    parser.add_argument(
        "--check",
        action="store_true",
        help="only print state, do not enqueue get_status",
    )
    args = parser.parse_args()

    engine = create_async_engine(_database_url(), pool_size=2)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if not args.check:
            repo = PostgresWahaSessionRepository(session_factory)
            service = WahaSessionService(
                sessions=repo, settings=load_waha_settings()
            )
            async with session_factory() as session:
                row = (
                    await session.execute(
                        text(
                            """
                            SELECT s.session_name
                              FROM waha_sessions s
                              JOIN users u ON u.id = s.user_id
                             WHERE u.phone_number = :phone
                            """
                        ),
                        {"phone": args.phone},
                    )
                ).first()
            if row is None:
                raise SystemExit(f"no WAHA session for {args.phone} — nothing to re-drive")
            outcome = await service.reconcile_session(row[0])
            print(f"get_status queued for {row[0]}: {outcome}")
            print("the agent round-trip is asynchronous; re-run with --check shortly.")
        await _state(session_factory, args.phone)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
