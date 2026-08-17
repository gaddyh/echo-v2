"""Green API webhook ingress route.

Static endpoint::

    POST /webhooks/whatsapp/green

No ``instance_id`` in the path. The payload contains ``instanceData.idInstance``,
so after authenticating we resolve the connection from that. This is what
enables create+configure in one partner call (we don't know ``idInstance``
until ``createInstance`` returns, so we can't construct an instance-specific
URL beforehand).

Auth (plan guardrail G3): Green sends the per-instance ``webhookUrlToken``
back in the ``Authorization`` header as ``Bearer <token>`` or
``Basic <base64(token:)>``. We store ``sha256(token)`` and validate with
``hmac.compare_digest``. The plaintext is never stored.

Idempotency (plan guardrail G8): providers deliver at-least-once. The route
dedupes by ``provider_message_id`` (in-memory for Step 0; durable with the
persistent repository). The dispatcher must never assume exactly-once.

The route does **only** ingress + auth + dedupe + dispatch. No business
logic. ``EventDispatcher`` is a small port with a stub impl this milestone;
the real ``ConversationService`` arrives in Step 2.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Protocol, runtime_checkable

from fastapi import APIRouter, Header, HTTPException, Request

from echo_v2.integrations.green.events import GreenEventAdapter
from echo_v2.persistence.whatsapp_connections import (
    InMemoryWhatsAppConnectionRepository,
    WhatsAppConnectionRepository,
)
from echo_v2.ports.whatsapp import (
    ProviderConnectionStateChanged,
    ProviderEvent,
    ProviderMessageEvent,
    ProviderMessageStatusEvent,
)

__all__ = [
    "EventDispatcher",
    "RecordingEventDispatcher",
    "build_router",
    "green_webhook_router",
]

_logger = logging.getLogger("echo_v2.app.webhooks.green")


@runtime_checkable
class EventDispatcher(Protocol):
    """Receiver of normalized provider events bound to a user."""

    async def dispatch(self, event: ProviderEvent, user_id: str) -> None: ...


class RecordingEventDispatcher:
    """Stub dispatcher for Step 0.

    Records every dispatched event (for tests and future wiring). Handles
    :class:`ProviderConnectionStateChanged` by updating the connection
    repository's stored status; other event types are recorded only -- the
    real ``ConversationService`` is added in Step 2.
    """

    def __init__(
        self,
        connection_repo: WhatsAppConnectionRepository | None = None,
    ) -> None:
        self._repo = connection_repo
        self.dispatched: list[tuple[ProviderEvent, str]] = []

    async def dispatch(self, event: ProviderEvent, user_id: str) -> None:
        self.dispatched.append((event, user_id))
        if isinstance(event, ProviderConnectionStateChanged) and self._repo is not None:
            await self._repo.update_status(
                event.connection,
                event.status,
                event.provider_raw_status,
            )


def _extract_token_from_header(authorization: str | None) -> str | None:
    """Extract the webhook token from a ``Bearer`` or ``Basic`` header.

    Green may send either form. Returns ``None`` if the header is missing or
    malformed -- the caller treats that as auth failure.
    """
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2:
        return None
    scheme, value = parts[0].lower(), parts[1].strip()
    if scheme == "bearer":
        return value
    if scheme == "basic":
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        # Green sends ``token:`` (empty password) or ``token:anything``.
        token = decoded.split(":", 1)[0]
        return token or None
    return None


def _valid_webhook_token(candidate: str, expected_hash: bytes) -> bool:
    """Constant-time comparison of ``sha256(candidate)`` to ``expected_hash``."""
    candidate_hash = hashlib.sha256(candidate.encode("utf-8")).digest()
    return hmac.compare_digest(candidate_hash, expected_hash)


def build_router(
    *,
    connection_repo: WhatsAppConnectionRepository,
    dispatcher: EventDispatcher,
    adapter: GreenEventAdapter | None = None,
) -> APIRouter:
    """Build a Green webhook router wired to the given dependencies.

    Kept as a factory so tests and the real app can inject fakes/real
    components. A module-level singleton (``green_webhook_router``) is also
    exposed for simple mounting, but the factory is the preferred entry point.
    """
    router = APIRouter()
    parse_adapter = adapter or GreenEventAdapter()

    # In-memory dedupe of provider_message_id (plan guardrail G8). Step 0 only;
    # a durable dedupe store ships with the persistent repository.
    seen_message_ids: set[str] = set()

    @router.post("/webhooks/whatsapp/green")
    async def green_webhook(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")

        instance_data = payload.get("instanceData")
        if (
            not isinstance(instance_data, dict)
            or instance_data.get("idInstance") is None
        ):
            raise HTTPException(status_code=404, detail="unknown instance")

        instance_id = str(instance_data["idInstance"])
        stored = await connection_repo.get_by_provider_id("green", instance_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="unknown instance")

        candidate = _extract_token_from_header(authorization)
        if candidate is None or not _valid_webhook_token(
            candidate, stored.webhook_token_hash
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

        event = parse_adapter.parse(payload)
        if event is None:
            return {"status": "ignored"}

        if isinstance(event, (ProviderMessageEvent, ProviderMessageStatusEvent)):
            if event.provider_message_id in seen_message_ids:
                return {"status": "duplicate"}
            seen_message_ids.add(event.provider_message_id)

        await dispatcher.dispatch(event, stored.user_id)
        return {"status": "received"}

    return router


# Module-level default router for simple mounting. Real deployments should
# prefer ``build_router`` with explicit dependency injection.
green_webhook_router = build_router(
    connection_repo=InMemoryWhatsAppConnectionRepository(),
    dispatcher=RecordingEventDispatcher(),
)
