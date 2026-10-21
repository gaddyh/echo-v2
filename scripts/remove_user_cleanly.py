"""Dry-run-first cleanup of an Echo user and their Baileys connection.

Usage:
    .venv/bin/python scripts/remove_user_cleanly.py --phone +972512217037
    .venv/bin/python scripts/remove_user_cleanly.py --phone +972512217037 --yes

The default mode is read-only. With ``--yes``, the script requires an exact
confirmation and then:

1. Logs the Baileys connection out of WhatsApp through the connector.
2. Deletes the connector connection, which cascades its auth state and data.
3. Deletes the Echo user, allowing Echo's foreign-key cascades to remove all
   user-owned records, including guardian-child links.

The Echo DATABASE_URL is loaded from ``echo-v2/.env``. The connector API URL
and token are loaded from ``BAILEYS_CONNECTOR_URL`` and
``BAILEYS_CONNECTOR_TOKEN``. If the token environment variable is absent, the
script also reads ``scripts/.baileys_connector_token``. That file is ignored
by git; its value must match the connector's ``INTERNAL_API_TOKEN``.

With ``--include-children``, children linked to the user through
``guardian_child_links`` are deleted as well, but only when no other guardian
link exists (of any status) and the child is not itself a guardian. Other
children are kept; their link to the deleted user is removed by cascade.
Children are deleted before the parent.

The script refuses to delete Echo data if connector logout or deletion fails.
A timeout after a connector write is reported as indeterminate and must be
checked manually before retrying.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_ROOT = Path(__file__).resolve().parent.parent
_TOKEN_FILE = Path(__file__).resolve().parent / ".baileys_connector_token"

# Connector tables checked in the preview and in post-deletion assertions.
# media_blobs has no FK in migration 004; migration 006 adds one. Until 006 is
# applied, the post-deletion assertion catches any orphaned blobs.
_CONNECTOR_TABLES = (
    "connections",
    "auth_credentials",
    "auth_keys",
    "identities",
    "groups",
    "group_participants",
    "event_inbox",
    "media_blobs",
)
# Column names that commonly reference users.id but may lack a foreign key.
# Used to warn about tables not covered by FK cascades (e.g. idempotency).
_USER_COLUMN_NAMES = ("user_id", "child_user_id", "guardian_user_id")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    query = parser.add_mutually_exclusive_group(required=True)
    query.add_argument("--phone", help="Exact users.phone_number to remove")
    query.add_argument("--name", help="Case-insensitive exact users.first_name")
    parser.add_argument(
        "--include-children",
        action="store_true",
        help=(
            "Also remove guardian_child_links children that have no other "
            "guardian link and are not guardians themselves"
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Perform deletion after the exact confirmation prompt",
    )
    return parser


def _normalize_phone(phone: str) -> str:
    digits = phone.strip().removeprefix("+")
    if not digits.isdigit() or not digits:
        raise ValueError(f"invalid phone: {phone!r}")
    return f"+{digits}"


def _async_database_url(url: str) -> str:
    """Use the installed psycopg3 driver even if .env uses bare PostgreSQL URL."""
    parts = urlsplit(url)
    if parts.scheme == "postgresql":
        return urlunsplit(
            ("postgresql+psycopg", parts.netloc, parts.path, parts.query, parts.fragment)
        )
    return url


def _connector_config() -> tuple[str, str]:
    url = os.getenv("BAILEYS_CONNECTOR_URL", "http://localhost:8080").rstrip("/")
    token = os.getenv("BAILEYS_CONNECTOR_TOKEN")
    if token is None and _TOKEN_FILE.exists():
        token = _TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError(
            "BAILEYS_CONNECTOR_TOKEN is missing and scripts/.baileys_connector_token "
            "does not exist"
        )
    return url, token


async def _scalar(conn: Any, query: str, params: dict[str, Any]) -> int:
    return int((await conn.execute(text(query), params)).scalar_one())


async def _find_users(engine: AsyncEngine, args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.phone:
        where = "u.phone_number = :value"
        value = _normalize_phone(args.phone)
    else:
        where = "u.first_name = :value"
        value = args.name
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT u.id::text AS id, u.phone_number, u.first_name, "
                "u.onboarding_status, u.account_status, u.created_at "
                f"FROM users u WHERE {where}"
            ),
            {"value": value},
        )
        return [dict(row) for row in result.mappings().all()]


async def _children_of(engine: AsyncEngine, guardian_id: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT u.id::text AS id, u.phone_number, u.first_name, "
                "u.onboarding_status, u.account_status, u.created_at "
                "FROM guardian_child_links l "
                "JOIN users u ON u.id = l.child_user_id "
                "WHERE l.guardian_user_id = :guardian_id"
            ),
            {"guardian_id": guardian_id},
        )
        children = [dict(row) for row in result.mappings().all()]
        for child in children:
            others = await conn.execute(
                text(
                    "SELECT guardian_user_id::text AS guardian_user_id, status "
                    "FROM guardian_child_links "
                    "WHERE child_user_id = :child_id AND guardian_user_id <> :guardian_id"
                ),
                {"child_id": child["id"], "guardian_id": guardian_id},
            )
            child["other_guardian_links"] = [dict(row) for row in others.mappings().all()]
            child["own_children_count"] = await _scalar(
                conn,
                "SELECT count(*) FROM guardian_child_links WHERE guardian_user_id = :child_id",
                {"child_id": child["id"]},
            )
    return children


async def _user_fk_targets(conn: Any) -> list[dict[str, str]]:
    """Discover all foreign keys referencing public.users via pg_constraint."""
    rows = await conn.execute(
        text(
            "SELECT con.conrelid::regclass::text AS table_name, "
            "       a.attname AS column_name, "
            "       CASE con.confdeltype WHEN 'c' THEN 'CASCADE' "
            "           WHEN 'n' THEN 'SET NULL' WHEN 'r' THEN 'RESTRICT' "
            "           WHEN 'a' THEN 'NO ACTION' WHEN 'd' THEN 'SET DEFAULT' "
            "       END AS on_delete "
            "FROM pg_constraint con "
            "JOIN pg_attribute a "
            "  ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1] "
            "WHERE con.contype = 'f' "
            "  AND con.confrelid = 'public.users'::regclass "
            "ORDER BY table_name, column_name"
        )
    )
    return [dict(row) for row in rows.mappings()]


async def _impact(
    engine: AsyncEngine, user_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Count rows referencing user_id via FK cascades and flag uncovered tables.

    Returns (cascade_impacts, uncovered) where cascade_impacts lists
    {"table", "column", "count", "on_delete"} for FK-covered references and
    uncovered lists {"table", "column", "count"} for user-like columns with no
    FK to users (e.g. idempotency_operations).
    """
    cascade_impacts: list[dict[str, Any]] = []
    uncovered: list[dict[str, Any]] = []
    async with engine.connect() as conn:
        fk_targets = await _user_fk_targets(conn)
        fk_pairs = set()
        for target in fk_targets:
            table = target["table_name"]
            column = target["column_name"]
            fk_pairs.add((table, column))
            count = await _scalar(
                conn,
                f'SELECT count(*) FROM {table} WHERE "{column}" = :user_id',
                {"user_id": user_id},
            )
            if count:
                cascade_impacts.append(
                    {
                        "key": f"{table}.{column}",
                        "count": count,
                        "on_delete": target["on_delete"],
                    }
                )

        all_columns = await conn.execute(
            text(
                "SELECT table_name, column_name "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                f"AND column_name IN ({', '.join(f':name{i}' for i in range(len(_USER_COLUMN_NAMES)))}) "
                "ORDER BY table_name, column_name"
            ),
            {f"name{i}": name for i, name in enumerate(_USER_COLUMN_NAMES)},
        )
        for row in all_columns.mappings():
            table = row["table_name"]
            column = row["column_name"]
            if (table, column) in fk_pairs:
                continue
            count = await _scalar(
                conn,
                f'SELECT count(*) FROM "{table}" WHERE "{column}" = :user_id',
                {"user_id": user_id},
            )
            if count:
                uncovered.append(
                    {"key": f"{table}.{column}", "count": count}
                )
    return cascade_impacts, uncovered


async def _connector_info(
    engine: AsyncEngine, connection_id: str
) -> dict[str, Any] | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT id::text, status, raw_status, phone_number, "
                "account_phone_number, updated_at "
                "FROM whatsapp_connector.connections WHERE id = :id"
            ),
            {"id": connection_id},
        )
        row = result.mappings().first()
        if row is None:
            return None
        info: dict[str, Any] = dict(row)
        for table in _CONNECTOR_TABLES[1:]:
            info[f"{table}_count"] = await _scalar(
                conn,
                f"SELECT count(*) FROM whatsapp_connector.\"{table}\" "
                "WHERE connection_id = :id",
                {"id": connection_id},
            )
        return info


async def _echo_connections(engine: AsyncEngine, user_id: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT provider, provider_connection_id, connection_status, "
                "provider_raw_status, updated_at "
                "FROM whatsapp_connections WHERE user_id = :user_id"
            ),
            {"user_id": user_id},
        )
        return [dict(row) for row in result.mappings().all()]


async def _connector_request(
    client: httpx.AsyncClient, method: str, url: str, connection_id: str
) -> None:
    response = await client.request(
        method,
        f"{url}/connections/{connection_id}{'/unpair' if method == 'POST' else ''}",
    )
    if response.status_code >= 400:
        detail = response.text[:300]
        raise RuntimeError(f"connector {method} failed ({response.status_code}): {detail}")


async def _remove_connector(
    client: httpx.AsyncClient, url: str, connection_id: str
) -> None:
    # Unpair first so WhatsApp receives the logout/unlink request. DELETE then
    # removes the connector row and cascades auth credentials and Signal keys.
    await _connector_request(client, "POST", url, connection_id)
    await _connector_request(client, "DELETE", url, connection_id)


async def _verify_deletion(
    engine: AsyncEngine, user_id: str, connection_ids: list[str]
) -> list[str]:
    """Assert that all user-owned and connector data is gone. Returns failures."""
    failures: list[str] = []
    async with engine.connect() as conn:
        remaining_user = await _scalar(
            conn, "SELECT count(*) FROM users WHERE id = :id", {"id": user_id}
        )
        if remaining_user:
            failures.append(f"users.id={user_id}: {remaining_user} row(s) still exist")

        remaining_conns = await _scalar(
            conn,
            "SELECT count(*) FROM whatsapp_connections WHERE user_id = :id",
            {"id": user_id},
        )
        if remaining_conns:
            failures.append(
                f"whatsapp_connections.user_id={user_id}: {remaining_conns} row(s) still exist"
            )

        remaining_links = await _scalar(
            conn,
            "SELECT count(*) FROM guardian_child_links "
            "WHERE guardian_user_id = :id OR child_user_id = :id",
            {"id": user_id},
        )
        if remaining_links:
            failures.append(
                f"guardian_child_links for user {user_id}: {remaining_links} row(s) still exist"
            )

        for cid in connection_ids:
            conn_count = await _scalar(
                conn,
                'SELECT count(*) FROM whatsapp_connector."connections" WHERE id = :id',
                {"id": cid},
            )
            if conn_count:
                failures.append(
                    f"whatsapp_connector.connections id={cid}: "
                    f"{conn_count} row(s) still exist"
                )
            for table in _CONNECTOR_TABLES[1:]:
                count = await _scalar(
                    conn,
                    f'SELECT count(*) FROM whatsapp_connector."{table}" '
                    "WHERE connection_id = :id",
                    {"id": cid},
                )
                if count:
                    failures.append(
                        f'whatsapp_connector.{table} connection_id={cid}: '
                        f"{count} row(s) still exist"
                    )
    return failures


def _print_preview(
    user: dict[str, Any],
    connections: list[dict[str, Any]],
    impacts: list[dict[str, Any]],
    uncovered: list[dict[str, Any]],
    connector_infos: dict[str, dict[str, Any] | None],
    heading: str = "User",
) -> None:
    print(heading)
    for key in ("id", "phone_number", "first_name", "onboarding_status", "account_status", "created_at"):
        print(f"  {key}: {user[key]}")
    print("Echo connections")
    if not connections:
        print("  (none)")
    for connection in connections:
        print(f"  provider={connection['provider']} id={connection['provider_connection_id']} "
              f"status={connection['connection_status']} raw={connection['provider_raw_status']}")
        info = connector_infos.get(str(connection["provider_connection_id"]))
        if info is not None:
            print("  Connector connection")
            for key in (
                "id", "status", "raw_status", "phone_number",
                "account_phone_number", "updated_at", "auth_credentials_count",
                "auth_keys_count", "identities_count", "groups_count",
                "group_participants_count", "event_inbox_count", "media_blobs_count",
            ):
                print(f"    {key}: {info.get(key)}")
        elif connection["provider"] == "baileys":
            print("  Connector connection: NOT FOUND")
    print("Echo cascade impact (FK-covered)")
    if not impacts:
        print("  (no rows found in FK-linked tables)")
    for entry in impacts:
        print(f"  {entry['key']}: {entry['count']} (on_delete={entry['on_delete']})")
    if uncovered:
        print("WARNING: user-linked columns without FK (will NOT cascade)")
        for entry in uncovered:
            print(f"  {entry['key']}: {entry['count']}")


def _print_children(children: list[dict[str, Any]]) -> None:
    print("Children")
    if not children:
        print("  (none linked to this guardian)")
        return
    for child in children:
        label = f"{child['first_name']} {child['phone_number']} ({child['id']})"
        if child["other_guardian_links"]:
            links = ", ".join(
                f"{link['guardian_user_id']} [{link['status']}]"
                for link in child["other_guardian_links"]
            )
            print(f"  KEEP {label} - other guardian link(s): {links}")
        elif child["own_children_count"]:
            print(f"  KEEP {label} - guardian of {child['own_children_count']} child(ren)")
        else:
            print(f"  REMOVE {label}")


async def main() -> None:
    args = _parser().parse_args()
    load_dotenv(_ROOT / ".env")
    if args.phone:
        args.phone = _normalize_phone(args.phone)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is missing from .env")
    engine = create_async_engine(_async_database_url(database_url))
    try:
        users = await _find_users(engine, args)
        if len(users) != 1:
            if not users:
                print("No exact user match found.")
            else:
                print(f"Refusing to continue: found {len(users)} exact matches.")
                for user in users:
                    print(f"  {user['id']} {user['phone_number']} {user['first_name']}")
            return

        user = users[0]

        children = await _children_of(engine, user["id"]) if args.include_children else []
        removable_children = [
            child
            for child in children
            if not child["other_guardian_links"] and not child["own_children_count"]
        ]
        targets = removable_children + [user]

        plans = []
        for target in targets:
            connections = await _echo_connections(engine, target["id"])
            impacts, uncovered = await _impact(engine, target["id"])
            connector_infos = {
                str(connection["provider_connection_id"]): await _connector_info(
                    engine, str(connection["provider_connection_id"])
                )
                for connection in connections
                if connection["provider"] == "baileys"
            }
            plans.append((target, connections, impacts, uncovered, connector_infos))

        _print_preview(*plans[-1][:5])
        if args.include_children:
            _print_children(children)
            for plan in plans[:-1]:
                print()
                _print_preview(*plan[:5], heading="Child to remove")

        if not args.yes:
            print("\nDry run only. Re-run with --yes to remove the listed user(s).")
            return

        confirmation = input(
            f"\nType DELETE {user['phone_number']} to confirm permanent removal: "
        )
        if confirmation != f"DELETE {user['phone_number']}":
            print("Confirmation did not match. Aborting.")
            return

        for target, connections, _impacts, _uncovered, _infos in plans:
            for connection in connections:
                if connection["provider"] != "baileys":
                    raise RuntimeError(
                        f"unsupported provider for clean removal: {connection['provider']} "
                        f"(user {target['id']})"
                    )

        connector_url, connector_token = _connector_config()
        headers = {"Authorization": f"Bearer {connector_token}"}
        timeout = httpx.Timeout(30.0, connect=5.0)
        async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
            for _target, connections, _impacts, _uncovered, _infos in plans:
                for connection in connections:
                    connection_id = str(connection["provider_connection_id"])
                    print(f"Cleaning Baileys connection {connection_id}...")
                    await _remove_connector(client, connector_url, connection_id)
                    print("  connector logout and deletion completed")

        async with engine.begin() as conn:
            for target, _connections, _impacts, _uncovered, _infos in plans:
                await conn.execute(
                    text("DELETE FROM users WHERE id = :user_id"),
                    {"user_id": target["id"]},
                )
                print(f"Deleted Echo user {target['id']} and cascaded user-owned data.")

        all_failures: list[str] = []
        for target, connections, _impacts, _uncovered, _infos in plans:
            connection_ids = [
                str(c["provider_connection_id"]) for c in connections
            ]
            failures = await _verify_deletion(engine, target["id"], connection_ids)
            if failures:
                label = f"{target['first_name']} {target['phone_number']} ({target['id']})"
                print(f"POST-DELETION ASSERTION FAILED for {label}:")
                for failure in failures:
                    print(f"  {failure}")
                all_failures.extend(failures)
        if all_failures:
            print(f"\n{len(all_failures)} assertion failure(s). Manual cleanup required.")
            raise RuntimeError("post-deletion assertion failed")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (RuntimeError, ValueError, httpx.HTTPError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
