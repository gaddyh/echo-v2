"""Submit the Baseline B Security and Exposure templates to 360dialog."""

from __future__ import annotations

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

TEMPLATE_CATEGORY = "UTILITY"
TEMPLATE_LANGUAGE = "he"
SECURITY_TEMPLATE_NAME = "guard_security_alert_he"
EXPOSURE_TEMPLATE_NAME = "guard_exposure_alert_he"


def template_payload(template_name: str = SECURITY_TEMPLATE_NAME) -> dict[str, object]:
    if template_name == SECURITY_TEMPLATE_NAME:
        header = "התראת בטיחות אישית"
        body = (
            "🚨 זוהה חשש לפגיעה ב{{1}}.\n\n"
            "{{2}}\n\n"
            "מומלץ לשוחח עם הילד/ה בהקדם."
        )
        example = ["שי", "בקבוצה זוהו הודעות הכוללות איומים המופנים אליו."]
    elif template_name == EXPOSURE_TEMPLATE_NAME:
        header = "עדכון על סביבת הקבוצה"
        body = (
            "👀 בקבוצת הוואטסאפ של {{1}} זוהתה התנהגות פוגענית.\n\n"
            "{{2}}.\n\n"
            "כדאי להיות מודעים לשיח בקבוצות."
        )
        example = ["הכיתה שלנו", "זוהתה השפלה חוזרת של אחד המשתתפים."]
    else:
        raise ValueError(f"unsupported template: {template_name}")

    return {
        "name": template_name,
        "category": TEMPLATE_CATEGORY,
        "language": TEMPLATE_LANGUAGE,
        "allow_category_change": True,
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": header},
            {
                "type": "BODY",
                "text": body,
                "example": {"body_text": [example]},
            },
            {"type": "FOOTER", "text": "Echo Guard"},
        ],
    }


def template_payloads() -> tuple[dict[str, object], dict[str, object]]:
    return template_payload(SECURITY_TEMPLATE_NAME), template_payload(EXPOSURE_TEMPLATE_NAME)


async def _submit(api_base_url: str, api_key: str, payload: dict[str, object]) -> None:
    import httpx

    url = f"{api_base_url}/message_templates"
    headers = {"D360-API-KEY": api_key, "Content-Type": "application/json"}
    name = payload["name"]
    print(f"Submitting template '{name}' to 360dialog...")
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
    print(f"HTTP {response.status_code}")
    try:
        print(json.dumps(response.json(), indent=2, ensure_ascii=False))
    except ValueError:
        print(response.text)
    if response.status_code not in (200, 201):
        raise RuntimeError(f"submission failed for {name}: HTTP {response.status_code}")


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("D360_API_KEY", "")
    if not api_key:
        print("ERROR: D360_API_KEY is not set. Add it to .env or export it.")
        sys.exit(1)
    api_base_url = os.environ.get(
        "D360_API_BASE_URL", "https://waba-v2.360dialog.io"
    ).rstrip("/")
    for payload in template_payloads():
        await _submit(api_base_url, api_key, payload)


if __name__ == "__main__":
    asyncio.run(main())
