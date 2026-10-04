"""Consent-confirmed manual activation of an existing Guard child link.

Usage:
    .venv/bin/python scripts/seed_guard_link.py \
        --guardian-phone ... --child-phone ... --consent-confirmed
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from echo_v2.persistence.identity import normalize_phone_e164


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--guardian-phone", required=True)
    parser.add_argument("--child-phone", required=True)
    parser.add_argument("--consent-confirmed", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> None:
    if not args.consent_confirmed:
        raise SystemExit("Refusing to activate Guard without --consent-confirmed")
    guardian_phone = normalize_phone_e164(args.guardian_phone)
    child_phone = normalize_phone_e164(args.child_phone)
    load_dotenv()
    database_url = os.environ["DATABASE_URL"]
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(database_url)
    now = datetime.now(timezone.utc)
    async with engine.begin() as connection:
        rows = await connection.execute(
            text("SELECT id, phone_number FROM users WHERE phone_number IN (:guardian, :child)"),
            {"guardian": guardian_phone, "child": child_phone},
        )
        users = {row.phone_number: str(row.id) for row in rows}
        if guardian_phone not in users or child_phone not in users:
            missing = [
                phone for phone in (guardian_phone, child_phone) if phone not in users
            ]
            raise SystemExit(f"Existing user not found for: {', '.join(missing)}")
        if users[guardian_phone] == users[child_phone]:
            raise SystemExit("Guardian and child must be different existing users")
        await connection.execute(
            text(
                "INSERT INTO guardian_child_links "
                "(guardian_user_id, child_user_id, status, child_consented_at, "
                "safety_enabled_at, updated_at) "
                "VALUES (:guardian, :child, 'active', :now, :now, :now) "
                "ON CONFLICT (guardian_user_id, child_user_id) DO UPDATE SET "
                "status = 'active', child_consented_at = :now, "
                "safety_enabled_at = :now, updated_at = :now"
            ),
            {"guardian": users[guardian_phone], "child": users[child_phone], "now": now},
        )
    await engine.dispose()
    print(f"Guard link active: guardian={users[guardian_phone]} child={users[child_phone]}")


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
