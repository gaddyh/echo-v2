"""Issue a one-time waiting-list link for a user, looked up by phone number.

Usage:
    .venv/bin/python scripts/issue_waiting_list_link.py <phone>

The phone is normalized to canonical E.164 (default region IL) before
lookup. Reads DATABASE_URL from .env. Prints the waiting-list URL.

Example:
    .venv/bin/python scripts/issue_waiting_list_link.py 0546610653
    .venv/bin/python scripts/issue_waiting_list_link.py +972546610653
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from echo_v2.persistence.identity import PhoneParseError, normalize_phone_e164

BASE_URL = "https://i-me.onrender.com"
LINK_TTL_HOURS = 48


async def main(raw_phone: str) -> None:
    try:
        phone = normalize_phone_e164(raw_phone)
    except PhoneParseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    load_dotenv()
    import os

    db_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(db_url)

    now = datetime.now(timezone.utc)

    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT id FROM users WHERE phone_number = :phone"),
            {"phone": phone},
        )
        user_id = result.scalar_one_or_none()
        if user_id is None:
            print(f"ERROR: no user found for phone {phone}", file=sys.stderr)
            return

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).digest()
        session_id = str(uuid.uuid4())
        expires_at = now + timedelta(hours=LINK_TTL_HOURS)

        await conn.execute(
            text(
                "INSERT INTO waiting_list_sessions "
                "(id, token_hash, user_id, expires_at) "
                "VALUES (:id, :hash, :uid, :exp)"
            ),
            {"id": session_id, "hash": token_hash, "uid": user_id, "exp": expires_at},
        )

        link = f"{BASE_URL}/q/{raw_token}"
        print(f"Waiting-list link for {phone} (expires in {LINK_TTL_HOURS}h):")
        print(link)

    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/issue_waiting_list_link.py <phone>", file=sys.stderr)
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
