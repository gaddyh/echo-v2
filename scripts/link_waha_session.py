"""Link a WAHA session to an Echo user (Phase 2 canary setup).

Creates or refreshes the ``whatsapp_connections`` row that maps a WAHA
batch-agent session (e.g. ``gaddy-test``) to an Echo user, so the WAHA
message consumer can route inbox rows into chat ingestion and the Guard
pipeline — the WAHA analogue of a Baileys connection row.

Usage:
    .venv/bin/python scripts/link_waha_session.py \
        --session gaddy-test --user-phone +9725...

Credentials are stored empty: Echo never calls WAHA (the agent pushes to
Echo), so no provider credentials or webhook token are needed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from echo_v2.persistence.credential_cipher import (
    CredentialCipher,
    IdentityCredentialCipher,
    LocalKeyCredentialCipher,
)
from echo_v2.persistence.identity import normalize_phone_e164

_PROVIDER = "waha"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True, help="WAHA session name")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--user-phone", help="Echo user phone (E164 or local)")
    group.add_argument("--user-id", help="Echo user UUID")
    parser.add_argument(
        "--status",
        default="connected",
        help="connection_status value (default: connected)",
    )
    return parser


async def _run(args: argparse.Namespace) -> None:
    load_dotenv()
    database_url = os.environ["DATABASE_URL"]
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(database_url)
    now = datetime.now(timezone.utc)

    # The connections repository decrypts credentials on every read; store a
    # valid (encrypted) placeholder so waha rows don't break the cipher path.
    credential_key = os.environ.get("ECHO_CREDENTIAL_KEY")
    cipher: CredentialCipher = (
        LocalKeyCredentialCipher(credential_key.encode("utf-8"))
        if credential_key
        else IdentityCredentialCipher()
    )
    credentials_blob = cipher.encrypt(b'{"provider": "waha"}')
    try:
        async with engine.begin() as connection:
            if args.user_id:
                row = (
                    await connection.execute(
                        text("SELECT id FROM users WHERE id = :user_id"),
                        {"user_id": args.user_id},
                    )
                ).one_or_none()
            else:
                phone = normalize_phone_e164(args.user_phone)
                row = (
                    await connection.execute(
                        text("SELECT id FROM users WHERE phone_number = :phone"),
                        {"phone": phone},
                    )
                ).one_or_none()
            if row is None:
                raise SystemExit("no matching Echo user — create the user first")
            user_id = row[0]

            existing = await connection.execute(
                text(
                    "SELECT id FROM whatsapp_connections "
                    "WHERE user_id = :user_id AND provider = :provider "
                    "AND provider_connection_id <> :session"
                ),
                {"user_id": user_id, "provider": _PROVIDER, "session": args.session},
            )
            if existing.one_or_none() is not None:
                raise SystemExit(
                    "user already linked to a different WAHA session — "
                    "unlink it first (one waha connection per user)"
                )

            result = await connection.execute(
                text(
                    """
                    INSERT INTO whatsapp_connections
                        (user_id, provider, provider_connection_id, credentials,
                         webhook_token_hash, connection_status, created_at, updated_at)
                    VALUES
                        (:user_id, :provider, :session, :credentials,
                         :webhook_token_hash, :status, :now, :now)
                    ON CONFLICT (provider, provider_connection_id) DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        credentials = EXCLUDED.credentials,
                        webhook_token_hash = EXCLUDED.webhook_token_hash,
                        connection_status = EXCLUDED.connection_status,
                        updated_at = EXCLUDED.updated_at
                    RETURNING id, (xmax = 0) AS inserted
                    """
                ),
                {
                    "user_id": user_id,
                    "provider": _PROVIDER,
                    "session": args.session,
                    "credentials": credentials_blob,
                    "webhook_token_hash": b"",
                    "status": args.status,
                    "now": now,
                },
            )
            linked = result.one()
            print(
                f"{'linked' if linked[1] else 're-linked'} WAHA session "
                f"{args.session!r} to user {user_id} "
                f"(connection {linked[0]}, status={args.status})"
            )
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
