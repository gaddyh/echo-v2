"""Add 2 test active waiting-for-me items for manual testing.

Usage:
    .venv/bin/python scripts/add_test_active.py

Reads DATABASE_URL from .env. Inserts:
- 2 chats (with activity_version=1)
- 2 waiting_for_me_results (decision=waiting_for_me)
- 2 waiting_for_me_active rows (target_version=1)

Uses 2 fake chat IDs with Hebrew names so you can see them in the digest.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

USER_ID = "bd1c584a-0d44-45c4-ac61-26e2b7415d5b"

# Two fake chats for testing.
TEST_CHATS = [
    {
        "chat_id": "972500000001@c.us",
        "chat_name": "יוסי",
        "last_text": "היי, אתה פנוי מחר?",
    },
    {
        "chat_id": "972500000002@c.us",
        "chat_name": "דנה",
        "last_text": "שלחת לי את הקובץ?",
    },
]


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

        for chat in TEST_CHATS:
            chat_id = chat["chat_id"]
            chat_name = chat["chat_name"]
            last_text = chat["last_text"]

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
                {"uid": USER_ID, "cid": chat_id, "cname": chat_name, "now": now},
            )

            # 2. Insert a waiting_for_me_result.
            result_id = str(uuid.uuid4())
            await conn.execute(
                text(
                    "INSERT INTO waiting_for_me_results "
                    "(id, user_id, chat_id, target_version, decision, reason) "
                    "VALUES (:id, :uid, :cid, 1, 'waiting_for_me', :reason)"
                ),
                {
                    "id": result_id,
                    "uid": USER_ID,
                    "cid": chat_id,
                    "reason": f"Test: {last_text}",
                },
            )

            # 3. Delete any existing active row for this chat, then insert.
            await conn.execute(
                text(
                    "DELETE FROM waiting_for_me_active "
                    "WHERE user_id = :uid AND chat_id = :cid"
                ),
                {"uid": USER_ID, "cid": chat_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO waiting_for_me_active "
                    "(id, user_id, chat_id, target_version, result_id, waiting_since) "
                    "VALUES (gen_random_uuid(), :uid, :cid, 1, :rid, :now)"
                ),
                {"uid": USER_ID, "cid": chat_id, "rid": result_id, "now": now},
            )

            print(f"Added: {chat_name} ({chat_id}) → result={result_id}")

    await engine.dispose()
    print("\nDone. 2 active items added.")


if __name__ == "__main__":
    asyncio.run(main())
