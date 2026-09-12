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
from langsmith import traceable

from echo_v2.app.webhooks.dedup import InMemoryWebhookDedupStore, WebhookDedupStore
from echo_v2.integrations.dialog360.events import Dialog360EventAdapter
from echo_v2.observability.sanitizers import (
    safe_webhook_inputs,
    safe_webhook_output,
)
from echo_v2.ports.bot import BotEventAdapter, BotEventType
from echo_v2.services.scheduling_flow import SchedulingFlowService

__all__ = ["build_router", "dialog360_webhook_router"]

_logger = logging.getLogger("echo_v2.app.webhooks.dialog360")


def build_router(
    *,
    flow_service: SchedulingFlowService,
    webhook_secret: str,
    adapter: BotEventAdapter | None = None,
    dedup_store: WebhookDedupStore | None = None,
    digest_reply_service=None,
    onboarding_service=None,
    feedback_handler=None,
) -> APIRouter:
    """Build a 360dialog bot webhook router.

    Args:
        flow_service: The scheduling flow service that processes events.
        webhook_secret: The bearer secret expected in the Authorization header.
        adapter: Event adapter (defaults to :class:`Dialog360EventAdapter`).
        dedup_store: Webhook dedup store (defaults to in-memory).
        digest_reply_service: Optional :class:`DigestReplyService` pre-handler.
            If it handles the event (returns ``True``), the flow service is
            skipped. Used for the "הצג הכול" button reply.
        onboarding_service: Optional :class:`OnboardingService`. If set,
            unknown users are routed to onboarding instead of being rejected.
            Also handles the name-collection step after connection.
        feedback_handler: Optional :class:`FeedbackHandler` pre-handler.
            Handles the feedback flyloop: template button taps, list item
            selections, feedback button callbacks, and miss reports.
    """
    router = APIRouter()
    parse_adapter = adapter or Dialog360EventAdapter()
    store = dedup_store or InMemoryWebhookDedupStore()
    has_secret = bool(webhook_secret)
    secret_hash = (
        hashlib.sha256(webhook_secret.encode("utf-8")).digest() if has_secret else b""
    )

    async def _handle_webhook(
        request: Request,
        authorization: str | None,
    ) -> dict[str, str]:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")

        # Auth: bearer token (optional — if no secret configured, skip auth).
        # 360dialog does not send Authorization headers by default; the URL
        # itself acts as the shared secret. Set D360_WEBHOOK_SECRET to enforce
        # bearer auth if your 360dialog plan supports it.
        if has_secret and not _valid_bearer(authorization, secret_hash):
            raise HTTPException(status_code=401, detail="unauthorized")

        # Parse the webhook into a canonical BotEvent.
        event = parse_adapter.parse(payload)
        if event is None:
            return {"status": "ignored"}

        # Deduplicate on event_id (wamid).
        if not await store.claim(event.event_id):
            return {"status": "duplicate"}

        # Pre-handler: feedback flyloop (template button, list, feedback buttons).
        if feedback_handler is not None:
            handled = await feedback_handler.handle(event)
            if handled:
                return {"status": "received"}

        # Pre-handler: digest reply ("הצג הכול" button).
        if digest_reply_service is not None:
            handled = await digest_reply_service.handle(event)
            if handled:
                return {"status": "received"}

        # Onboarding pre-handler: if the user is unknown or in onboarding,
        # route to the onboarding service instead of the flow service.
        if onboarding_service is not None:
            is_onboarding = await onboarding_service.is_onboarding(event.user_phone)
            if is_onboarding:
                # Handle name response (after connection) or resend request.
                if event.type is BotEventType.TEXT and event.text:
                    handled = await onboarding_service.handle_name_response(
                        event.user_phone, event.text
                    )
                    if handled:
                        return {"status": "received"}
                    if event.text.strip() == "קוד":
                        await onboarding_service.handle_resend_request(event.user_phone)
                        return {"status": "received"}
                return {"status": "received"}

            # Check if user is fully unknown — start onboarding.
            user_info = await flow_service._user_resolver.resolve(event.user_phone)
            if user_info is None:
                await onboarding_service.handle_unknown_user(event.user_phone)
                return {"status": "received"}

        # Dispatch to the flow service.
        await flow_service.handle(event)
        return {"status": "received"}

    @router.post("/webhooks/bot/dialog360")
    @traceable(
        name="wfm.webhook.dialog360",
        process_inputs=safe_webhook_inputs,
        process_outputs=safe_webhook_output,
    )
    async def dialog360_webhook(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        return await _handle_webhook(request, authorization)

    @router.post("/webhook/360dialog")
    @traceable(
        name="wfm.webhook.dialog360",
        process_inputs=safe_webhook_inputs,
        process_outputs=safe_webhook_output,
    )
    async def dialog360_webhook_alt(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        return await _handle_webhook(request, authorization)

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
