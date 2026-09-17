#!/usr/bin/env python3
"""Test the consent-first onboarding flow against real services.

Sends the intro message (or info, or start) to a real phone via 360dialog,
using the real OnboardingService wiring. No webhook involved — calls the
service directly.

Usage:
    .venv/bin/python scripts/test_onboarding_intro.py <phone> <action>

Actions:
    intro   — send the introduction with both buttons (default)
    info    — send the explanation with the connect button
    start   — trigger start_onboarding (creates user + provisions Green!)
    text    — simulate a text event "hello" (same as intro)
    consent — simulate a text event "חברו אותי" (same as start)

Examples:
    .venv/bin/python scripts/test_onboarding_intro.py +972546610653 intro
    .venv/bin/python scripts/test_onboarding_intro.py +972546610653 info

Reads DATABASE_URL, ECHO_CREDENTIAL_KEY, GREEN_API_PARTNER_TOKEN,
D360_API_KEY from .env.

WARNING: "start" and "consent" create a real user row and provision a
real Green instance. Use only if you intend to go through onboarding.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv()

from echo_v2.integrations.dialog360.client import Dialog360Client
from echo_v2.integrations.dialog360.settings import Dialog360Settings
from echo_v2.integrations.green.client import GreenClient
from echo_v2.integrations.green.provisioner import GreenProvisioner
from echo_v2.integrations.green.settings import load_settings as load_green_settings
from echo_v2.persistence.compose import build_postgres_repos
from echo_v2.persistence.settings import load_db_settings
from echo_v2.persistence.user_repository import PostgresUserRepository
from echo_v2.ports.bot import BotEvent, BotEventType
from echo_v2.services.onboarding import OnboardingService


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test the consent-first onboarding flow against real services."
    )
    parser.add_argument("phone", help="Recipient phone in E.164 (e.g. +972546610653)")
    parser.add_argument(
        "action",
        choices=["intro", "info", "start", "text", "consent"],
        default="intro",
        nargs="?",
        help="What to send (default: intro)",
    )
    args = parser.parse_args()

    # Wire up real components (same as app/main.py).
    settings = load_db_settings()
    repos = build_postgres_repos(settings)

    d360_settings = Dialog360Settings()
    d360_client = Dialog360Client(settings=d360_settings)

    green_settings = load_green_settings()
    green_client = GreenClient(settings=green_settings)

    user_repo = PostgresUserRepository(repos.session_factory)
    provisioner = GreenProvisioner(
        client=green_client,
        credential_resolver=repos.connections,
    )
    webhook_base_url = os.environ.get(
        "ECHO_WEBHOOK_BASE_URL", "https://i-me.onrender.com"
    )
    service = OnboardingService(
        bot=d360_client,
        user_repo=user_repo,
        connection_repo=repos.connections,
        provisioner=provisioner,
        green_client=green_client,
        webhook_base_url=webhook_base_url,
    )

    # Build the event for the requested action.
    action = args.action
    if action == "intro":
        print(f"Sending introduction to {args.phone} ...")
        await service.send_introduction(args.phone)
        print("Done. Check WhatsApp for the intro with two buttons.")
    elif action == "info":
        print(f"Sending explanation to {args.phone} ...")
        await service.send_explanation(args.phone)
        print("Done. Check WhatsApp for the explanation + connect button.")
    elif action == "start":
        print(f"Triggering start_onboarding for {args.phone} ...")
        print("WARNING: This creates a user row + provisions a Green instance.")
        await service.start_onboarding(args.phone)
        print("Done. Check WhatsApp for the 'please wait' message, then the OTP.")
    elif action == "text":
        event = BotEvent(
            event_id="test-intro",
            user_phone=args.phone,
            type=BotEventType.TEXT,
            text="hello",
        )
        print(f"Sending TEXT 'hello' event to {args.phone} ...")
        await service.handle_unknown_event(event)
        print("Done. Check WhatsApp for the intro with two buttons.")
    elif action == "consent":
        event = BotEvent(
            event_id="test-consent",
            user_phone=args.phone,
            type=BotEventType.TEXT,
            text="חברו אותי",
        )
        print(f"Sending TEXT 'חברו אותי' event to {args.phone} ...")
        print("WARNING: This creates a user row + provisions a Green instance.")
        await service.handle_unknown_event(event)
        print("Done. Check WhatsApp for the 'please wait' message, then the OTP.")

    await d360_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
