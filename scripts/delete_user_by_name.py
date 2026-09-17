"""Dry-run: find users by first_name in the production DB.

Usage:
    .venv/bin/python scripts/delete_user_by_name.py <name>

Reads DATABASE_URL from .env. Prints matching users and their related
row counts (connections, chats, waiting items, etc.) so you can see the
cascade impact before deleting. Does NOT delete anything.

Pass --yes to actually delete (after reviewing the dry-run output).
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Tables that cascade on user delete (for impact preview).
CASCADE_TABLES = [
    "whatsapp_connections",
    "chats",
    "contacts",
    "messages",
    "chat_mutes",
    "chat_not_interested_clicks",
    "scheduled_actions",
    "waiting_for_me_active",
    "waiting_for_me_actions",
    "waiting_for_me_feedback",
    "waiting_list_sessions",
    "waiting_for_me_results",
]


async def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    name = args[0]
    do_delete = "--yes" in args

    load_dotenv()
    import os

    db_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(db_url)

    async with engine.begin() as conn:
        # Find users by first_name (case-sensitive exact match).
        result = await conn.execute(
            text(
                "SELECT id, phone_number, first_name, onboarding_status, "
                "account_status, created_at "
                "FROM users WHERE first_name = :name"
            ),
            {"name": name},
        )
        rows = result.fetchall()

    if not rows:
        print(f"No users found with first_name = {name!r}")
        await engine.dispose()
        return

    print(f"Found {len(rows)} user(s) with first_name = {name!r}:\n")
    for r in rows:
        uid, phone, fname, onboarding, account, created = r
        print(f"  user_id:        {uid}")
        print(f"  phone:          {phone}")
        print(f"  first_name:     {fname}")
        print(f"  onboarding:     {onboarding}")
        print(f"  account_status: {account}")
        print(f"  created_at:     {created}")

        # Count related rows that will cascade-delete.
        async with engine.begin() as conn2:
            print("  cascade impact:")
            for table in CASCADE_TABLES:
                cnt = await conn2.execute(
                    text(f"SELECT count(*) FROM {table} WHERE user_id = :uid"),
                    {"uid": uid},
                )
                n = cnt.scalar_one()
                if n:
                    print(f"    {table}: {n}")
        print()

    if not do_delete:
        print("Dry-run only. To actually delete, re-run with --yes.")
        await engine.dispose()
        return

    # Confirm interactively.
    confirm = input(
        f"Type the exact name '{name}' to confirm deletion of "
        f"{len(rows)} user(s) and ALL cascaded data: "
    )
    if confirm != name:
        print("Confirmation did not match. Aborting.")
        await engine.dispose()
        return

    async with engine.begin() as conn:
        for r in rows:
            uid = r[0]
            await conn.execute(
                text("DELETE FROM users WHERE id = :uid"),
                {"uid": uid},
            )
            print(f"Deleted user {uid}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
