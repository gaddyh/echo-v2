#!/usr/bin/env python3
"""Connect a user's Green API instance manually.

Usage:
    python scripts/connect_green.py <phone> <id_instance> <api_token> [--webhook-token TOKEN]

Inserts (or updates) a whatsapp_connections row for the user identified by
their phone number. The api_token is encrypted with ECHO_CREDENTIAL_KEY
before storage.

Prerequisites:
    - User must already exist in the users table.
    - ECHO_CREDENTIAL_KEY must be set in .env or environment.
    - DATABASE_URL must be set in .env or environment.

Example:
    python scripts/connect_green.py +972546610653 1101234567 abc123token
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path

# Ensure src/ is on the path when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from echo_v2.persistence.credential_cipher import LocalKeyCredentialCipher
from echo_v2.persistence.orm import UserRow, WhatsAppConnectionRow
from echo_v2.persistence.whatsapp_connections import (
    StoredConnection,
)
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ConnectionStatus,
    ProviderCredentials,
)
from echo_v2.persistence.postgres_whatsapp_connections import (
    PostgresWhatsAppConnectionRepository,
)
from echo_v2.persistence.settings import load_db_settings
import os


async def main() -> None:
    parser = argparse.ArgumentParser(description="Connect a Green API instance to a user.")
    parser.add_argument("phone", help="User's phone number in E.164 format (e.g. +972546610653)")
    parser.add_argument("id_instance", help="Green API idInstance")
    parser.add_argument("api_token", help="Green API apiTokenInstance")
    parser.add_argument(
        "--webhook-token",
        default=None,
        help="Webhook token for Green webhooks (random string). If omitted, one is generated.",
    )
    args = parser.parse_args()

    # Normalize phone (basic — just ensure it starts with +).
    phone = args.phone if args.phone.startswith("+") else f"+{args.phone}"

    # Webhook token.
    webhook_token = args.webhook_token or os.urandom(16).hex()
    webhook_token_hash = hashlib.sha256(webhook_token.encode("utf-8")).digest()

    # Load settings + cipher.
    settings = load_db_settings()
    if settings.credential_key is None:
        print("ERROR: ECHO_CREDENTIAL_KEY is not set. Generate one with:", file=sys.stderr)
        print('  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"', file=sys.stderr)
        sys.exit(1)

    url = settings.database_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    engine = create_async_engine(url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # 1. Find the user by phone.
    async with session_factory() as session:
        stmt = select(UserRow.id, UserRow.phone_number).where(UserRow.phone_number == phone)
        result = await session.execute(stmt)
        row = result.first()
        if row is None:
            print(f"ERROR: No user found with phone {phone}.", file=sys.stderr)
            print("Create the user first.", file=sys.stderr)
            await engine.dispose()
            sys.exit(1)
        user_id = str(row.id)
        print(f"Found user: id={user_id} phone={row.phone_number}")

    # 2. Save the connection.
    cipher = LocalKeyCredentialCipher(settings.credential_key)
    repo = PostgresWhatsAppConnectionRepository(session_factory, cipher)

    ref = ConnectionRef(provider="green", provider_connection_id=args.id_instance)
    credentials = ProviderCredentials(data=args.api_token.encode("utf-8"))
    conn = StoredConnection(
        user_id=user_id,
        ref=ref,
        credentials=credentials,
        webhook_token_hash=webhook_token_hash,
        status=ConnectionStatus.CONNECTED,
        provider_raw_status="authorized",
    )
    await repo.save(conn)
    await engine.dispose()

    print(f"\n✅ Green API connection saved:")
    print(f"   user_id:              {user_id}")
    print(f"   id_instance:          {args.id_instance}")
    print(f"   api_token:            {'*' * len(args.api_token)} (encrypted)")
    print(f"   webhook_token:        {webhook_token}")
    print(f"   webhook_token_hash:   {webhook_token_hash.hex()}")
    print(f"\n   Set this webhook token in your Green instance settings:")
    print(f"   {webhook_token}")
    print(f"\n   Green webhook URL: https://<your-app>.onrender.com/webhooks/whatsapp/green")


if __name__ == "__main__":
    asyncio.run(main())
