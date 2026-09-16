"""Submit the snooze_reminder_v1 UTILITY template to 360dialog.

Creates a new WhatsApp template for the snooze reminder. The reminder
is sent when a snoozed waiting-for-me item's snooze period expires.

Template structure:
  Name:     snooze_reminder_v1
  Category: UTILITY
  Language: he
  Header:   "תזכורת Echo"
  Body:     "🔔 תזכורת: {{1}} עדיין ממתין למענה."
  Footer:   "Echo — לא מפספסים שיחה חשובה"
  Button:   URL "טיפול בממתינים" → https://i-me.onrender.com/q/{{1}}

At runtime:
  Body {{1}}    → contact display name (e.g. "שיוש")
  URL  {{1}}    → opaque waiting-list session token (passed as url_suffix
                  to Dialog360Client.send_template)

The URL button opens the waiting-list mini-app, where the user can
handle, snooze, or dismiss the item. This is consistent with the
morning digest template (morning_waiting_digest6).

Usage:
    .venv/bin/python scripts/create_snooze_reminder_template.py

Reads D360_API_KEY and D360_API_BASE_URL from .env.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

TEMPLATE_NAME = "snooze_reminder_v1"
TEMPLATE_CATEGORY = "UTILITY"
TEMPLATE_LANGUAGE = "he"
BASE_URL = "https://i-me.onrender.com"

TEMPLATE_PAYLOAD = {
    "name": TEMPLATE_NAME,
    "category": TEMPLATE_CATEGORY,
    "language": TEMPLATE_LANGUAGE,
    "allow_category_change": True,
    "components": [
        {
            "type": "HEADER",
            "format": "TEXT",
            "text": "תזכורת Echo",
        },
        {
            "type": "BODY",
            "text": "🔔 תזכורת: {{1}} עדיין ממתין למענה.",
            "example": {
                "body_text": [
                    ["שיוש"]
                ]
            },
        },
        {
            "type": "FOOTER",
            "text": "Echo — לא מפספסים שיחה חשובה",
        },
        {
            "type": "BUTTONS",
            "buttons": [
                {
                    "type": "URL",
                    "text": "טיפול בממתינים",
                    "url": f"{BASE_URL}/q/{{{{1}}}}",
                    "example": [
                        f"{BASE_URL}/q/example_token_abc123xyz",
                    ],
                },
            ],
        },
    ],
}


async def main() -> None:
    load_dotenv()

    api_key = os.environ.get("D360_API_KEY", "")
    if not api_key:
        print("ERROR: D360_API_KEY not set. Add it to .env or export it.")
        sys.exit(1)

    api_base_url = os.environ.get(
        "D360_API_BASE_URL", "https://waba-v2.360dialog.io"
    )
    url = f"{api_base_url}/message_templates"

    import httpx

    headers = {
        "D360-API-KEY": api_key,
        "Content-Type": "application/json",
    }

    print(f"Submitting template '{TEMPLATE_NAME}' to 360dialog...")
    print(f"  URL: {url}")
    print(f"  Category: {TEMPLATE_CATEGORY}")
    print(f"  Language: {TEMPLATE_LANGUAGE}")
    print(f"  Button: URL → {BASE_URL}/q/{{1}}")
    print()

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        try:
            response = await client.post(url, json=TEMPLATE_PAYLOAD, headers=headers)
        except httpx.ConnectError as exc:
            print(f"ERROR: connect error: {exc}")
            sys.exit(1)
        except httpx.ReadTimeout as exc:
            print(f"ERROR: read timeout: {exc}")
            sys.exit(1)

    print(f"HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        print(f"Response body:\n{response.text}")
        return

    print(json.dumps(body, indent=2, ensure_ascii=False))

    if response.status_code in (200, 201):
        status = body.get("status") if isinstance(body, dict) else None
        print(f"\nTemplate submitted. Status: {status or 'pending'}")
        print("Check approval status in the 360dialog dashboard.")
    else:
        print(f"\nERROR: submission failed (HTTP {response.status_code})")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
