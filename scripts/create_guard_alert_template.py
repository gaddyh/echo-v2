"""Submit the guard_alert_v1 UTILITY template to 360dialog.

The template is used for proactive Echo Guard notifications. It carries one
safe, deterministic summary parameter and a dynamic URL button to the
guardian-scoped Guard Review page.

Template structure:
  Name:     guard_alert_v1
  Category: UTILITY
  Language: he
  Header:   "התראת בטיחות מ־Echo"
  Body:     "Echo זיהה דפוס שייתכן שחשוב לשים לב אליו:\n{{1}}\n\nפתח/י את Guard Review לפרטים."
  Footer:   "Echo Guard — שמים לב בזמן"
  Button:   URL "לפתיחת Guard Review" → {BASE_URL}/q/{token}

Usage:
    .venv/bin/python scripts/create_guard_alert_template.py

Reads D360_API_KEY and D360_API_BASE_URL from .env. The API key is never
printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

TEMPLATE_NAME = "guard_alert_v1"
TEMPLATE_CATEGORY = "UTILITY"
TEMPLATE_LANGUAGE = "he"
DEFAULT_BASE_URL = "https://i-me.onrender.com"


def template_payload(base_url: str) -> dict[str, object]:
    return {
        "name": TEMPLATE_NAME,
        "category": TEMPLATE_CATEGORY,
        "language": TEMPLATE_LANGUAGE,
        "allow_category_change": True,
        "components": [
            {
                "type": "HEADER",
                "format": "TEXT",
                "text": "התראת בטיחות מ־Echo",
            },
            {
                "type": "BODY",
                "text": (
                    "Echo זיהה דפוס שייתכן שחשוב לשים לב אליו:\n"
                    "{{1}}\n\n"
                    "פתח/י את Guard Review לפרטים."
                ),
                "example": {
                    "body_text": [
                        ["ייתכן שנדרשת תשומת לב בשיחה."],
                    ]
                },
            },
            {
                "type": "FOOTER",
                "text": "Echo Guard — שמים לב בזמן",
            },
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL",
                        "text": "לפתיחת Guard Review",
                        "url": f"{base_url}/q/{{{{1}}}}",
                        "example": [f"{base_url}/q/example_guard_token"],
                    }
                ],
            },
        ],
    }


async def main() -> None:
    load_dotenv()

    api_key = os.environ.get("D360_API_KEY", "")
    if not api_key:
        print("ERROR: D360_API_KEY is not set. Add it to .env or export it.")
        sys.exit(1)

    api_base_url = os.environ.get(
        "D360_API_BASE_URL", "https://waba-v2.360dialog.io"
    ).rstrip("/")
    base_url = os.environ.get("ECHO_WEBHOOK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    payload = template_payload(base_url)

    import httpx

    url = f"{api_base_url}/message_templates"
    headers = {
        "D360-API-KEY": api_key,
        "Content-Type": "application/json",
    }

    print(f"Submitting template '{TEMPLATE_NAME}' to 360dialog...")
    print(f"  URL: {url}")
    print(f"  Category: {TEMPLATE_CATEGORY}")
    print(f"  Language: {TEMPLATE_LANGUAGE}")
    print(f"  Review URL: {base_url}/q/{{token}}")
    print()

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
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
