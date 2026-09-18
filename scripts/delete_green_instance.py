"""Delete a Green API instance by id via the partner deleteInstanceAccount.

Usage:
    .venv/bin/python scripts/delete_green_instance.py <id_instance> [<id_instance> ...]

Use this to clean up Green instances that are orphaned (no matching DB
user/connection row), e.g. after a user was deleted without the Green-side
cleanup succeeding.

Reads GREEN_API_PARTNER_TOKEN (and optionally GREEN_API_PARTNER_URL) from
.env. Best-effort: prints the outcome per instance and continues to the next
on failure.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Ensure src/ is on the path when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

from echo_v2.integrations.green.client import GreenClient
from echo_v2.integrations.green.settings import load_settings as load_green_settings


async def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    load_dotenv()
    client = GreenClient(load_green_settings())
    try:
        for instance_id in args:
            try:
                await client.delete_instance(instance_id)
                print(f"deleted Green instance {instance_id}")
            except Exception as exc:  # noqa: BLE001 — best-effort, continue
                print(f"FAILED to delete Green instance {instance_id}: {exc}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
