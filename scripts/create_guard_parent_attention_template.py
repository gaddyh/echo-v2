"""Submit the no-link Baseline B parent-attention template to 360dialog."""

from __future__ import annotations

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

TEMPLATE_NAME = "guard_parent_attention_v1"
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
                "type": "HEADER",
                "format": "TEXT",
                "text": "תשומת לב לגבי הילד/ה",
            },
            {
                "type": "BODY",
                "text": (
                    "נראה שיש משהו שכדאי לדעת על {{1}}:\n"
                    "{{2}}\n\n"
                    "בשיחה: {{3}}\n"
                    "Echo Guard"
                ),
                "example": {
                    "body_text": [[
                        "שי",
                        "נראה שיש לחץ חוזר מצד ילדים אחרים.",
                        "קבוצת הכיתה",
                    ]]
                },
            },
            {
                "type": "FOOTER",
                "text": "Echo Guard",
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
    import httpx

    url = f"{api_base_url}/message_templates"
    headers = {"D360-API-KEY": api_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        response = await client.post(url, json=template_payload(), headers=headers)
    print(f"HTTP {response.status_code}")
    try:
        print(json.dumps(response.json(), indent=2, ensure_ascii=False))
    except ValueError:
        print(response.text)
    if response.status_code not in (200, 201):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
