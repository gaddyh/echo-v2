#!/usr/bin/env python3
"""Drive the full WAHA onboarding E2E against the deployed Echo app.

Simulates the 360dialog bot webhook (real payload shapes, real bearer
secret) so the DEPLOYED app runs the entire flow: consent → name →
connect click → WAHA allocation + create_and_start command → (the live
agent executes) → QR event → QR delivered to the phone → user scans →
WORKING → connected.

Usage:
    .venv/bin/python scripts/e2e_waha_onboarding.py <phone> <step> [value]
      consent  — send the consent text "חברו אותי"
      name     — send the name reply (value = the name)
      connect  — click the [חבר אותי] button (onboarding:connect)
      showqr   — click the [הצצ QR] button (onboarding:show_qr)
      qr       — send the 'qr' keyword
      status   — print current DB state for the user

Reads DATABASE_URL and D360_WEBHOOK_SECRET from .env.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.environ.get("ECHO_WEBHOOK_BASE_URL", "https://i-me.onrender.com")


def _post_webhook(message: dict, phone: str) -> dict:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "e2e-test-entry",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "972500000000"},
                            "contacts": [{"profile": {"name": "E2E"}, "wa_id": phone.lstrip("+")}],
                            "messages": [message],
                        }
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/webhooks/bot/dialog360",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['D360_WEBHOOK_SECRET']}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return {"status": r.status, "body": r.read().decode()[:200]}


def _text(phone: str, text: str) -> dict:
    return _post_webhook(
        {
            "from": phone.lstrip("+"),
            "id": f"e2e-{uuid.uuid4().hex[:16]}",
            "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
            "text": {"body": text},
            "type": "text",
        },
        phone,
    )


def _button(phone: str, button_id: str) -> dict:
    return _post_webhook(
        {
            "from": phone.lstrip("+"),
            "id": f"e2e-{uuid.uuid4().hex[:16]}",
            "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": button_id, "title": "button"},
            },
        },
        phone,
    )


async def _status(phone: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    url = os.environ["DATABASE_URL"].replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        user = (
            await conn.execute(
                text("SELECT id, onboarding_status FROM users WHERE phone_number = :p"),
                {"p": phone},
            )
        ).one_or_none()
        if user is None:
            print("no user row")
            return
        print(f"user {user[0]} status={user[1]}")
        conn_row = (
            await conn.execute(
                text(
                    "SELECT provider, provider_connection_id, connection_status,"
                    " provider_raw_status FROM whatsapp_connections"
                    " WHERE user_id = :u ORDER BY updated_at DESC"
                ),
                {"u": user[0]},
            )
        ).all()
        for c in conn_row:
            print(f"  connection: {tuple(c)}")
        if conn_row:
            session_name = conn_row[0][1]
            sess = (
                await conn.execute(
                    text(
                        "SELECT session_name, worker_id, status, raw_status,"
                        " last_status_at, pending_effect, (last_qr_image IS NOT NULL)"
                        " FROM waha_sessions WHERE session_name = :s"
                    ),
                    {"s": session_name},
                )
            ).one_or_none()
            print(f"  waha_session: {tuple(sess) if sess else None}")
            cmds = (
                await conn.execute(
                    text(
                        "SELECT action, status, attempts, last_error FROM waha_commands"
                        " WHERE session_name = :s ORDER BY created_at"
                    ),
                    {"s": session_name},
                )
            ).all()
            for c in cmds:
                print(f"  command: {tuple(c)}")
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phone")
    parser.add_argument("step", choices=["consent", "name", "connect", "showqr", "qr", "status"])
    parser.add_argument("value", nargs="?", default=None)
    args = parser.parse_args()

    if args.step == "consent":
        print(_text(args.phone, "חברו אותי"))
    elif args.step == "name":
        print(_text(args.phone, args.value or "E2E Test"))
    elif args.step == "connect":
        print(_button(args.phone, "onboarding:connect"))
    elif args.step == "showqr":
        print(_button(args.phone, "onboarding:show_qr"))
    elif args.step == "qr":
        print(_text(args.phone, "qr"))
    elif args.step == "status":
        asyncio.run(_status(args.phone))


if __name__ == "__main__":
    main()
