"""Submit the Echo Guard WhatsApp reconnect template to 360dialog."""

from __future__ import annotations

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

TEMPLATE_NAME = "guard_reconnect_v1"
TEMPLATE_CATEGORY = "UTILITY"
TEMPLATE_LANGUAGE = "he"


def template_payload() -> dict[str, object]:
    return {
        "name": TEMPLATE_NAME,
        "category": TEMPLATE_CATEGORY,
        "language": TEMPLATE_LANGUAGE,
        "allow_category_change": True,
        "components": [
            {
                "type": "BODY",
                "text": (
                    "החיבור של {{1}} ל-Echo Guard נותק.\n\n"
                    "כדי להמשיך בהגנה, יש לחבר מחדש את ה-WhatsApp של הילד/ה."
                ),
                "example": {"body_text": [["אוריה"]]},
            },
            {"type": "FOOTER", "text": "Echo Guard"},
            {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "חיבור מחדש"}]},
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
    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        response = await client.post(
            f"{api_base_url}/message_templates",
            json=template_payload(),
            headers={"D360-API-KEY": api_key, "Content-Type": "application/json"},
        )
    print(f"HTTP {response.status_code}")
    try:
        print(json.dumps(response.json(), indent=2, ensure_ascii=False))
    except ValueError:
        print(response.text)
    if response.status_code not in (200, 201):
        raise RuntimeError(f"submission failed: HTTP {response.status_code}")


if __name__ == "__main__":
    asyncio.run(main())
