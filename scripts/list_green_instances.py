"""List all live Green API instances via the partner getInstances endpoint.

``getInstances`` can return stale entries for instances that were already
deleted on Green's side. This script calls ``getStateInstance`` on each
to filter out the dead ones and show only live instances.

Usage:
    .venv/bin/python scripts/list_green_instances.py

Reads GREEN_API_PARTNER_TOKEN from .env.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv()

from echo_v2.integrations.green.client import GreenApiError, GreenClient
from echo_v2.integrations.green.settings import load_settings


async def _is_live(client: GreenClient, iid: str, token: str) -> str | None:
    """Return the state string if the instance is live, or None if dead (404)."""
    try:
        return await client.get_state_instance(iid, token)
    except GreenApiError:
        return None


async def main() -> None:
    settings = load_settings()
    client = GreenClient(settings)
    try:
        instances = await client.get_instances()
        print(f"getInstances returned {len(instances)} entries, checking liveness...\n")

        live: list[tuple[str, str, str]] = []  # (id, token_preview, state)
        dead: list[str] = []

        for inst in instances:
            if not isinstance(inst, dict):
                continue
            iid = str(inst.get("idInstance", "?"))
            token = str(inst.get("apiTokenInstance", ""))
            token_preview = token[:8] + "..." if token else "?"
            state = await _is_live(client, iid, token)
            if state is None:
                dead.append(iid)
            else:
                live.append((iid, token_preview, state))

        print(f"Live instances ({len(live)}):")
        for iid, token_preview, state in live:
            print(f"  idInstance: {iid}  apiToken: {token_preview}  state: {state}")

        if dead:
            print(f"\nDead/stale entries ({len(dead)}):")
            for iid in dead:
                print(f"  idInstance: {iid}")

        print(f"\nTotal: {len(live)} live, {len(dead)} stale")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
