"""360dialog webhook ingress route for the Echo Business Bot.

Static endpoint::

    POST /webhooks/bot/dialog360

Auth: a shared bearer secret (``D360_WEBHOOK_SECRET``). 360dialog sends
it back in the ``Authorization`` header as ``Bearer <secret>``. We
compare with ``hmac.compare_digest`` to avoid timing attacks.

Idempotency: the route deduplicates on ``event.event_id`` (the
WhatsApp message ID ``wamid.*``). 360dialog retries webhooks, so without
dedup the flow service would process the same message multiple times.

The route does **only** ingress + auth + dedupe + dispatch to the
:class:`SchedulingFlowService`. No business logic here.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from echo_v2.app.webhooks.dedup import InMemoryWebhookDedupStore, WebhookDedupStore
from echo_v2.integrations.dialog360.events import Dialog360EventAdapter
from echo_v2.ports.bot import BotEventAdapter
from echo_v2.services.scheduling_flow import SchedulingFlowService

__all__ = ["build_router", "dialog360_webhook_router"]

_logger = logging.getLogger("echo_v2.app.webhooks.dialog360")


def build_router(
    *,
    flow_service: SchedulingFlowService,
    webhook_secret: str,
    adapter: BotEventAdapter | None = None,
    dedup_store: WebhookDedupStore | None = None,
) -> APIRouter:
    """Build a 360dialog bot webhook router.

    Args:
        flow_service: The scheduling flow service that processes events.
        webhook_secret: The bearer secret expected in the Authorization header.
        adapter: Event adapter (defaults to :class:`Dialog360EventAdapter`).
        dedup_store: Webhook dedup store (defaults to in-memory).
    """
    router = APIRouter()
    parse_adapter = adapter or Dialog360EventAdapter()
    store = dedup_store or InMemoryWebhookDedupStore()
    secret_hash = hashlib.sha256(webhook_secret.encode("utf-8")).digest()

    @router.post("/webhooks/bot/dialog360")
    async def dialog360_webhook(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")

        # Auth: bearer token.
        if not _valid_bearer(authorization, secret_hash):
            raise HTTPException(status_code=401, detail="unauthorized")

        # Parse the webhook into a canonical BotEvent.
        event = parse_adapter.parse(payload)
        if event is None:
            return {"status": "ignored"}

        # Deduplicate on event_id (wamid).
        if not await store.claim(event.event_id):
            return {"status": "duplicate"}

        # Dispatch to the flow service.
        await flow_service.handle(event)
        return {"status": "received"}

    return router


def _valid_bearer(authorization: str | None, expected_hash: bytes) -> bool:
    """Constant-time validation of a bearer token against the hash."""
    if not authorization:
        return False
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    candidate = parts[1].strip()
    candidate_hash = hashlib.sha256(candidate.encode("utf-8")).digest()
    return hmac.compare_digest(candidate_hash, expected_hash)


# Module-level default router for simple mounting.
# Real deployments should prefer build_router with explicit DI.
dialog360_webhook_router = build_router(
    flow_service=None,  # type: ignore[arg-type]
    webhook_secret="",
)
