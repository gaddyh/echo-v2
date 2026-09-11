"""Send the full digest as individual cards (bypasses template).

Usage:
    .venv/bin/python scripts/send_digest.py

Reads DATABASE_URL, D360_API_KEY from .env.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from echo_v2.integrations.dialog360.client import Dialog360Client
from echo_v2.integrations.dialog360.settings import Dialog360Settings
from echo_v2.persistence.compose import build_postgres_repos
from echo_v2.persistence.contacts import PostgresContactRepository
from echo_v2.persistence.settings import load_db_settings

# Production user (from prior testing).
USER_ID = "bd1c584a-0d44-45c4-ac61-26e2b7415d5b"
USER_PHONE = "972546610653"


async def main() -> None:
    load_dotenv()

    settings = load_db_settings()
    repos = build_postgres_repos(settings)

    # Load active items.
    all_active = await repos.wfm_active.list_all_for_user(user_id=USER_ID)
    now = datetime.now(timezone.utc)
    current = []
    for active in all_active:
        chat = await repos.chat_state.get(USER_ID, active.chat_id)
        if chat is None or chat.activity_version != active.target_version:
            print(f"  SKIP (stale): {active.chat_id} v={active.target_version}")
            continue
        if active.snoozed_until is not None and active.snoozed_until > now:
            print(f"  SKIP (snoozed): {active.chat_id}")
            continue
        if await repos.chat_mutes.is_muted(
            user_id=USER_ID, chat_id=active.chat_id, now=now
        ):
            print(f"  SKIP (muted): {active.chat_id}")
            continue
        current.append(active)

    # Sort oldest first, max 5.
    current.sort(key=lambda a: a.waiting_since)
    current = current[:5]

    print(f"Active items: {len(current)}")
    if not current:
        print("No active items to send.")
        return

    # Build + send cards.
    d360_settings = Dialog360Settings()
    client = Dialog360Client(d360_settings)
    try:
        # Send header text.
        await client.send_text(
            USER_PHONE,
            f"בוקר טוב גדי 👋\n\nEcho מצא {len(current)} שיחות שאולי מחכות לתגובה שלך.",
        )
        print("Sent header.")

        contact_repo = PostgresContactRepository(repos.session_factory)
        total = len(current)
        for i, active in enumerate(current, 1):
            phone = active.chat_id.split("@")[0] if "@" in active.chat_id else active.chat_id
            chat = await repos.chat_state.get(USER_ID, active.chat_id)
            chat_name = chat.chat_name if chat else None

            if not chat_name:
                contact = await contact_repo.find_by_phone(USER_ID, phone)
                chat_name = contact.display_name if contact else None

            msg = await repos.messages.get_latest_inbound(
                user_id=USER_ID, chat_id=active.chat_id
            )
            last_text = msg.text if msg and msg.text else None
            if not chat_name and msg:
                chat_name = msg.chat_name or msg.sender_name

            display_name = chat_name or phone
            body = f'{i} מתוך {total}\n\n{display_name}\n"{last_text or "שלח/ה הודעה"}"'

            buttons = [
                {
                    "id": f"menu_action:{active.chat_id}:{active.target_version}",
                    "title": "מה לעשות",
                },
                {
                    "id": f"menu_feedback:{active.chat_id}:{active.target_version}",
                    "title": "משוב ל־Echo",
                },
            ]

            msg_id = await client.send_buttons(
                USER_PHONE,
                body_text=body,
                buttons=buttons,
            )
            print(f"  Card {i}/{total}: {display_name} → {msg_id}")

        print(f"\nSent {total} cards.")
    except Exception as exc:
        print(f"\nSend failed: {exc}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
