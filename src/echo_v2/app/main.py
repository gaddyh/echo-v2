"""Echo v2 application bootstrap.

Wires the full dependency graph and creates the FastAPI app:

* Postgres persistence layer (connections, idempotency, scheduled actions).
* Green API messaging (sends scheduled messages from the user's WhatsApp).
* 360dialog bot channel (Echo Business Bot — receives commands, sends
  confirmations).
* SchedulingFlowService (3-step state machine: vCard → message → time).
* SchedulingService + Scheduler (executes due actions idempotently).
* Webhook routes: Green (user's WhatsApp events) + 360dialog (bot).

Env vars (all required in production):
    DATABASE_URL              — postgresql+psycopg://...
    ECHO_CREDENTIAL_KEY       — Fernet key for credential encryption
    ECHO_DEFAULT_PHONE_REGION — ISO 3166-1 alpha-2 (default: IL)
    GREEN_API_PARTNER_TOKEN   — Green partner API token
    GREEN_API_PARTNER_URL     — Green API base (default: https://api.green-api.com)
    D360_API_KEY              — 360dialog bot phone-number API key
    D360_WEBHOOK_SECRET       — bearer secret for 360dialog webhook auth
    OPENAI_API_KEY            — for LLM time parser fallback
"""

from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI

from echo_v2.app.webhooks.dialog360 import build_router as build_dialog360_router
from echo_v2.app.webhooks.green import (
    RecordingEventDispatcher,
)
from echo_v2.app.webhooks.green import (
    build_router as build_green_router,
)
from echo_v2.integrations.dialog360.client import Dialog360Client
from echo_v2.integrations.dialog360.events import Dialog360EventAdapter
from echo_v2.integrations.dialog360.settings import Dialog360Settings
from echo_v2.integrations.green.client import GreenClient
from echo_v2.integrations.green.messaging import GreenMessaging
from echo_v2.integrations.green.settings import load_settings as load_green_settings
from echo_v2.observability import InMemoryEventSink
from echo_v2.persistence.compose import build_postgres_repos
from echo_v2.persistence.conversation_state import InMemoryConversationStateRepository
from echo_v2.persistence.settings import load_db_settings
from echo_v2.persistence.user_resolver import PostgresUserResolver
from echo_v2.runtime.idempotency import InMemoryIdempotencyStore
from echo_v2.services.scheduler import Scheduler
from echo_v2.services.scheduling import SchedulingService
from echo_v2.services.scheduling_flow import SchedulingFlowService
from echo_v2.services.time_parser import CombinedTimeParser, LLMTimeParser

__all__ = ["create_app"]

_logger = logging.getLogger("echo_v2.app")
logging.basicConfig(level=logging.INFO)


class _SessionUserResolver:
    """Adapts :class:`PostgresUserResolver` to the :class:`UserResolver` protocol.

    Creates a fresh session per ``resolve`` call from the shared session
    factory, so the resolver is safe to use across requests without holding
    a long-lived session.
    """

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def resolve(self, phone: str) -> tuple[str, str] | None:
        async with self._session_factory() as session:
            resolver = PostgresUserResolver(session)
            return await resolver.resolve(phone)


def create_app() -> FastAPI:
    """Build the fully wired FastAPI application."""
    load_dotenv()

    # --- persistence -------------------------------------------------------
    db_settings = load_db_settings()
    repos = build_postgres_repos(db_settings)

    # --- Green (user's WhatsApp — sends scheduled messages) ---------------
    green_settings = load_green_settings()
    green_client = GreenClient(
        partner_api_url=green_settings.partner_api_url,
        partner_token=green_settings.partner_token,
    )
    green_messaging = GreenMessaging(
        client=green_client,
        credential_resolver=repos.connections,
    )

    # --- 360dialog (Echo Business Bot — conversational interface) ---------
    d360_settings = Dialog360Settings()
    d360_client = Dialog360Client(settings=d360_settings)

    # --- scheduling service (executes due actions) ------------------------
    # In-memory idempotency for now — a Postgres implementation exists but
    # we use in-memory to keep the bootstrap simple. The scheduler's
    # idempotency is primarily enforced by the ScheduledAction status
    # machine (claim → in_progress → succeeded) plus this store.
    idempotency_store = InMemoryIdempotencyStore()
    scheduling_service = SchedulingService(
        action_repo=repos.scheduled_actions,
        connection_repo=repos.connections,
        messaging=green_messaging,
        idempotency_store=idempotency_store,
        event_sink=InMemoryEventSink(),
    )

    # --- time parser (regex first, LLM fallback) --------------------------
    llm_parser = LLMTimeParser(
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        model=os.environ.get("LLM_MODEL_NAME", "gpt-4.1"),
    )
    time_parser = CombinedTimeParser(llm_parser=llm_parser)

    # --- scheduling flow service (3-step bot conversation) ----------------
    flow_service = SchedulingFlowService(
        bot=d360_client,
        state_repo=InMemoryConversationStateRepository(),
        scheduling_service=scheduling_service,
        time_parser=time_parser,
        user_resolver=_SessionUserResolver(repos.session_factory),
    )

    # --- scheduler (background poller) -------------------------------------
    scheduler = Scheduler(
        service=scheduling_service,
        action_repo=repos.scheduled_actions,
        lease_seconds=float(os.environ.get("SCHEDULER_LEASE_SECONDS", "300")),
        poll_interval_seconds=float(os.environ.get("SCHEDULER_POLL_INTERVAL", "5")),
    )

    # --- FastAPI app -------------------------------------------------------
    app = FastAPI(title="Echo v2", version="0.1.0")

    # Green webhook: receives events from the user's WhatsApp (delivery
    # status, connection state changes). Static URL — the instance is
    # resolved from the payload.
    green_router = build_green_router(
        connection_repo=repos.connections,
        dispatcher=RecordingEventDispatcher(connection_repo=repos.connections),
    )
    app.include_router(green_router)

    # 360dialog webhook: receives messages from the Echo Business Bot
    # (user commands, vCards, time replies).
    dialog360_router = build_dialog360_router(
        flow_service=flow_service,
        webhook_secret=d360_settings.webhook_secret,
        adapter=Dialog360EventAdapter(),
    )
    app.include_router(dialog360_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # --- scheduler background task -----------------------------------------
    @app.on_event("startup")
    async def _start_scheduler() -> None:
        asyncio.create_task(_run_scheduler(scheduler))

    return app


async def _run_scheduler(scheduler: Scheduler) -> None:
    """Run the scheduler loop, recovering stale actions on startup."""
    try:
        recovered = await scheduler.recover()
        if recovered:
            _logger.info("recovered %d stale scheduled actions", recovered)
    except Exception:
        _logger.exception("scheduler recovery failed on startup")
    await scheduler.run_loop()


# Module-level app for uvicorn: `uvicorn echo_v2.app.main:app`
# Created lazily so importing the module doesn't fail without env vars
# (e.g. during test collection). uvicorn will call create_app() on import.
def _get_app() -> FastAPI:
    return create_app()


app = None  # type: ignore[assignment]

# When uvicorn imports this module, it expects `app` to be a FastAPI instance.
# We create it only if DATABASE_URL is set (production). For local dev without
# a database, use `uvicorn echo_v2.app.main:create_app --factory`.
if os.environ.get("DATABASE_URL"):
    app = create_app()
