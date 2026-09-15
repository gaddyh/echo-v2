"""Create 3 test active waiting-for-me items and issue a waiting-list link.

Usage:
    .venv/bin/python scripts/add_test_active_3.py [base_url]

Defaults to http://localhost:8000 for local testing.
Reads DATABASE_URL from .env.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

USER_ID = "bd1c584a-0d44-45c4-ac61-26e2b7415d5b"

# 3 contacts with different scenarios for testing labels/tags/filters.
CONTACTS = [
    {
        "chat_id": "972512217037@c.us",
        "chat_name": "שיוש",
        "last_text": "היי, מתי נדבר? צריך לתאם פגישה",
        "summary": "שיוש רוצה לתאם פגישה ומחכה לתשובה",
    },
    {
        "chat_id": "972500000099@c.us",
        "chat_name": "דמי אבירם",
        "last_text": "שלחתי לך את המסמכים, תכתוב כשתראה",
        "summary": "דמי שלח מסמכים ומחכה לאישור קבלה",
    },
    {
        "chat_id": "972500000088@c.us",
        "chat_name": "מירי כהן",
        "last_text": "יש לי שאלה לגבי החשבונית, אפשר לדבר?",
        "summary": "מירי צריכה עזרה עם חשבונית ומחכה לתשובה",
    },
]


async def main() -> None:
    load_dotenv()
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    db_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(db_url)

    now = datetime.now(timezone.utc)

    async with engine.begin() as conn:
        # Verify user exists.
        result = await conn.execute(
            text("SELECT id FROM users WHERE id = :uid"),
            {"uid": USER_ID},
        )
        if result.scalar_one_or_none() is None:
            print(f"ERROR: user {USER_ID} not found")
            return

        for c in CONTACTS:
            chat_id = c["chat_id"]
            chat_name = c["chat_name"]
            last_text = c["last_text"]
            summary = c["summary"]

            # 1. Upsert chat row.
            await conn.execute(
                text(
                    "INSERT INTO chats (user_id, chat_id, chat_name, "
                    "activity_version, last_message_at, last_direction, "
                    "next_analysis_at, last_processed_version) "
                    "VALUES (:uid, :cid, :cname, 1, :now, 'inbound', NULL, 1) "
                    "ON CONFLICT (user_id, chat_id) DO UPDATE SET "
                    "activity_version = 1, "
                    "chat_name = :cname, "
                    "last_message_at = :now, "
                    "last_direction = 'inbound', "
                    "next_analysis_at = NULL, "
                    "last_processed_version = 1"
                ),
                {"uid": USER_ID, "cid": chat_id, "cname": chat_name, "now": now},
            )

            # 2. Delete existing active + result rows, then insert fresh.
            await conn.execute(
                text(
                    "DELETE FROM waiting_for_me_active "
                    "WHERE user_id = :uid AND chat_id = :cid"
                ),
                {"uid": USER_ID, "cid": chat_id},
            )
            await conn.execute(
                text(
                    "DELETE FROM waiting_for_me_results "
                    "WHERE user_id = :uid AND chat_id = :cid"
                ),
                {"uid": USER_ID, "cid": chat_id},
            )
            result_id = str(uuid.uuid4())
            await conn.execute(
                text(
                    "INSERT INTO waiting_for_me_results "
                    "(id, user_id, chat_id, target_version, decision, reason, summary) "
                    "VALUES (:id, :uid, :cid, 1, 'waiting_for_me', :reason, :summary)"
                ),
                {
                    "id": result_id,
                    "uid": USER_ID,
                    "cid": chat_id,
                    "reason": f"Test: {last_text}",
                    "summary": summary,
                },
            )

            # 3. Insert a fresh active row.
            await conn.execute(
                text(
                    "INSERT INTO waiting_for_me_active "
                    "(id, user_id, chat_id, target_version, result_id, waiting_since) "
                    "VALUES (gen_random_uuid(), :uid, :cid, 1, :rid, :now)"
                ),
                {"uid": USER_ID, "cid": chat_id, "rid": result_id, "now": now},
            )

            print(f"Added: {chat_name} ({chat_id})")

        # 4. Issue a waiting-list session token.
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).digest()
        session_id = str(uuid.uuid4())
        expires_at = now + timedelta(hours=48)

        await conn.execute(
            text(
                "INSERT INTO waiting_list_sessions "
                "(id, token_hash, user_id, expires_at) "
                "VALUES (:id, :hash, :uid, :exp)"
            ),
            {"id": session_id, "hash": token_hash, "uid": USER_ID, "exp": expires_at},
        )

        link = f"{base_url}/q/{raw_token}"
        print("\nWaiting-list link (one-time, expires in 48h):")
        print(link)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
