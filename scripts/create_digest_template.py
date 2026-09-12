"""Submit the morning_waiting_digest6 UTILITY template to 360dialog.

Creates a new WhatsApp template for approval. The template is based on
the existing morning_waiting_digest4, but replaces the Quick Reply
button with a dynamic URL button that opens the waiting-list mini web
app.

Template structure:
  Name:     morning_waiting_digest6
  Category: UTILITY
  Language: he
  Header:   "סיכום הבוקר של Echo"
  Body:     "בוקר טוב {{1}} 👋\n\nEcho מצא {{2}} שיחות שאולי מחכות לתגובה שלך."
  Footer:   "Echo — לא מפספסים שיחה חשובה"
  Button:   URL "טיפול בממתינים" → https://i-me.onrender.com/q/{{1}}

At runtime, the URL button's {{1}} is filled with the opaque waiting-list
session token (passed as url_suffix to Dialog360Client.send_template).

Usage:
    .venv/bin/python scripts/create_digest_template.py

Reads D360_API_KEY and D360_API_BASE_URL from .env.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

TEMPLATE_NAME = "morning_waiting_digest6"
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
            "text": "סיכום הבוקר של Echo",
        },
        {
            "type": "BODY",
            "text": "בוקר טוב {{1}} 👋\n\nEcho מצא {{2}} שיחות שאולי מחכות לתגובה שלך.",
            "example": {
                "body_text": [
                    ["גדי", "3"]
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
