"""Dry-run: find users by first_name OR phone_number in the production DB.

Usage:
    .venv/bin/python scripts/delete_user_by_name.py <name_or_phone>

The argument is treated as a phone number if it starts with ``+`` or is all
digits (after an optional leading ``+``); otherwise it is treated as a
case-sensitive exact match on ``users.first_name``.

Reads DATABASE_URL and GREEN_API_PARTNER_TOKEN from .env. Prints matching
users, their related row counts (connections, chats, waiting items, etc.),
and any Green API instance owned by the user so you can see the cascade
impact before deleting. Does NOT delete anything.

Pass --yes to actually delete (after reviewing the dry-run output). When
deleting, the user's Green API instance is also deleted via the partner
``deleteInstance`` endpoint (best-effort — DB deletion proceeds even if
Green is unreachable).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Ensure src/ is on the path when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from echo_v2.integrations.green.client import GreenClient
from echo_v2.integrations.green.settings import load_settings as load_green_settings

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
    "daily_digests",
]


def _is_phone(arg: str) -> bool:
    """True if ``arg`` looks like a phone number (E.164-ish)."""
    digits = arg.removeprefix("+")
    return digits.isdigit() and len(digits) > 0


def _normalize_phone(arg: str) -> str:
    """Ensure the phone starts with ``+`` (E.164 form stored in users.phone_number)."""
    return arg if arg.startswith("+") else f"+{arg}"


async def _delete_green_instance(green_client: GreenClient, instance_id: str) -> None:
    """Best-effort partner ``deleteInstance``. Never raises."""
    try:
        await green_client.delete_instance(instance_id)
        print(f"    green: deleted instance {instance_id}")
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup
        print(f"    green: FAILED to delete instance {instance_id}: {exc}")


async def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    query = args[0]
    do_delete = "--yes" in args

    load_dotenv()

    is_phone = _is_phone(query)
    if is_phone:
        phone = _normalize_phone(query)
        where_clause = "users.phone_number = :q"
        bind_value: str = phone
        label = f"phone_number = {phone!r}"
    else:
        where_clause = "users.first_name = :q"
        bind_value = query
        label = f"first_name = {query!r}"

    # Green client is only needed for actual deletion, but construct it up
    # front so a missing GREEN_API_PARTNER_TOKEN fails fast and clearly.
    green_client: GreenClient | None = None
    if do_delete:
        green_client = GreenClient(load_green_settings())

    engine = create_async_engine(__import__("os").environ["DATABASE_URL"])

    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT id, phone_number, first_name, onboarding_status, "
                "account_status, created_at "
                f"FROM users WHERE {where_clause}"
            ),
            {"q": bind_value},
        )
        rows = result.fetchall()

    if not rows:
        print(f"No users found with {label}")
        await engine.dispose()
        if green_client is not None:
            await green_client.aclose()
        return

    print(f"Found {len(rows)} user(s) with {label}:\n")
    for r in rows:
        uid, phone, fname, onboarding, account, created = r
        print(f"  user_id:        {uid}")
        print(f"  phone:          {phone}")
        print(f"  first_name:     {fname}")
        print(f"  onboarding:     {onboarding}")
        print(f"  account_status: {account}")
        print(f"  created_at:     {created}")

        # Green API instance owned by this user (from whatsapp_connections).
        async with engine.begin() as conn2:
            conn_result = await conn2.execute(
                text(
                    "SELECT provider, provider_connection_id, connection_status "
                    "FROM whatsapp_connections WHERE user_id = :uid"
                ),
                {"uid": uid},
            )
            conn_rows = conn_result.fetchall()

        if conn_rows:
            print("  green instance:")
            for c in conn_rows:
                provider, provider_id, status = c
                print(f"    provider={provider} id={provider_id} status={status}")
        else:
            print("  green instance: (none)")

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
        if green_client is not None:
            await green_client.aclose()
        return

    # Confirm interactively.
    confirm = input(
        f"Type the exact query '{query}' to confirm deletion of "
        f"{len(rows)} user(s), their Green instances, and ALL cascaded data: "
    )
    if confirm != query:
        print("Confirmation did not match. Aborting.")
        await engine.dispose()
        if green_client is not None:
            await green_client.aclose()
        return

    async with engine.begin() as conn:
        for r in rows:
            uid = r[0]

            # 1. Delete the user's Green instance via the partner API
            #    (best-effort) before removing the connection row.
            if green_client is not None:
                conn_result = await conn.execute(
                    text(
                        "SELECT provider_connection_id FROM whatsapp_connections "
                        "WHERE user_id = :uid AND provider = 'green'"
                    ),
                    {"uid": uid},
                )
                for (provider_id,) in conn_result.fetchall():
                    if provider_id:
                        await _delete_green_instance(green_client, provider_id)

            # 2. Clean up any pool rows claimed by this user (no FK cascade).
            await conn.execute(
                text(
                    "DELETE FROM green_instance_pool WHERE claimed_by_user_id = :uid"
                ),
                {"uid": uid},
            )

            # 3. Delete the user (cascades to whatsapp_connections, chats, ...).
            await conn.execute(
                text("DELETE FROM users WHERE id = :uid"),
                {"uid": uid},
            )
            print(f"Deleted user {uid}")

    await engine.dispose()
    if green_client is not None:
        await green_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
