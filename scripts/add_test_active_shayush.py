"""Create a test active waiting-for-me item for שיוש and issue a waiting-list link.

Usage:
    .venv/bin/python scripts/add_test_active_shayush.py

Reads DATABASE_URL from .env. Inserts:
- 1 chat (activity_version=1, chat_name=שיוש)
- 1 waiting_for_me_result (decision=waiting_for_me)
- 1 waiting_for_me_active row (target_version=1)
- 1 waiting_list_session (token) for the user

Prints the waiting-list URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

USER_ID = "bd1c584a-0d44-45c4-ac61-26e2b7415d5b"
CHAT_ID = "972512217037@c.us"
CHAT_NAME = "שיוש"
LAST_TEXT = "היי, מתי נדבר?"
BASE_URL = "https://i-me.onrender.com"


async def main() -> None:
    load_dotenv()
    import os

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

        # 1. Upsert chat row (activity_version=1).
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
            {"uid": USER_ID, "cid": CHAT_ID, "cname": CHAT_NAME, "now": now},
        )

        # 2. Insert a waiting_for_me_result.
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
                "cid": CHAT_ID,
                "reason": f"Test: {LAST_TEXT}",
                "summary": "שיוש שואל מתי תדברו",
            },
        )

        # 3. Delete any existing active row for this chat, then insert.
        await conn.execute(
            text(
                "DELETE FROM waiting_for_me_active "
                "WHERE user_id = :uid AND chat_id = :cid"
            ),
            {"uid": USER_ID, "cid": CHAT_ID},
        )
        await conn.execute(
            text(
                "INSERT INTO waiting_for_me_active "
                "(id, user_id, chat_id, target_version, result_id, waiting_since) "
                "VALUES (gen_random_uuid(), :uid, :cid, 1, :rid, :now)"
            ),
            {"uid": USER_ID, "cid": CHAT_ID, "rid": result_id, "now": now},
        )

        print(f"Added: {CHAT_NAME} ({CHAT_ID}) → result={result_id}")

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

        link = f"{BASE_URL}/q/{raw_token}"
        print(f"\nWaiting-list link (one-time, expires in 48h):")
        print(link)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
