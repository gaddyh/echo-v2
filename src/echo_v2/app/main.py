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
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

from echo_v2.app.webhooks.dialog360 import build_router as build_dialog360_router
from echo_v2.app.webhooks.green import (
    ChatEventDispatcher,
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
from echo_v2.observability import LoggingEventSink
from echo_v2.persistence.compose import build_postgres_repos
from echo_v2.persistence.conversation_state import InMemoryConversationStateRepository
from echo_v2.persistence.settings import load_db_settings
from echo_v2.persistence.user_resolver import PostgresUserResolver
from echo_v2.services.chat_analysis_worker import (
    ChatAnalysisProcessor,
    ChatAnalysisWorker,
)
from echo_v2.services.chat_ingestion import ChatIngestionService
from echo_v2.services.scheduler import Scheduler
from echo_v2.services.scheduling import SchedulingService
from echo_v2.services.scheduling_flow import SchedulingFlowService
from echo_v2.services.time_parser import CombinedTimeParser, LLMTimeParser

__all__ = ["create_app"]

_logger = logging.getLogger("echo_v2.app")
logging.basicConfig(level=logging.INFO)


def _build_openai_client():
    """Build a single shared OpenAI client, optionally LangSmith-wrapped.

    When ``LANGSMITH_TRACING=true``, the client is wrapped with
    ``langsmith.wrappers.wrap_openai`` so every chat completion becomes
    a child span under the current trace.

    Returns ``(client, raw_client)`` where ``raw_client`` is the
    underlying ``AsyncOpenAI`` that must be closed on shutdown. When
    tracing is disabled, ``client is raw_client``.

    When ``OPENAI_API_KEY`` is empty, a dummy key is used so the client
    constructs without error — actual API calls will fail, but the app
    can still boot (e.g. for tests or when LLM features are disabled).
    """
    from openai import AsyncOpenAI

    api_key = os.environ.get("OPENAI_API_KEY", "") or "dummy-key-for-boot"
    raw_client = AsyncOpenAI(api_key=api_key)

    tracing_enabled = os.environ.get("LANGSMITH_TRACING", "false").lower() in (
        "1", "true", "yes",
    )
    if tracing_enabled:
        # Enforce privacy: never send raw LLM inputs/outputs to LangSmith.
        # These may contain message text, phone numbers, or other PII.
        # The @traceable sanitizers hash IDs, but the LLM call itself
        # captures the full prompt/completion unless we hide them.
        os.environ.setdefault("LANGSMITH_HIDE_INPUTS", "true")
        os.environ.setdefault("LANGSMITH_HIDE_OUTPUTS", "true")

        from langsmith.wrappers import wrap_openai

        traced_client = wrap_openai(raw_client)
        _logger.info("OpenAI client wrapped with LangSmith tracing")
        return traced_client, raw_client

    return raw_client, raw_client


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

    # --- OpenAI client (shared, optionally LangSmith-wrapped) -------------
    from echo_v2.observability.privacy import ensure_hash_key_or_fail

    ensure_hash_key_or_fail()
    openai_client, raw_openai_client = _build_openai_client()

    # --- Green (user's WhatsApp — sends scheduled messages) ---------------
    green_settings = load_green_settings()
    green_client = GreenClient(settings=green_settings)
    green_messaging = GreenMessaging(
        client=green_client,
        credential_resolver=repos.connections,
    )

    # --- 360dialog (Echo Business Bot — conversational interface) ---------
    d360_settings = Dialog360Settings()
    d360_client = Dialog360Client(settings=d360_settings)

    # --- onboarding service (OTP-based WhatsApp onboarding) ---------------
    from echo_v2.integrations.green.provisioner import GreenProvisioner
    from echo_v2.persistence.user_repository import PostgresUserRepository
    from echo_v2.services.onboarding import OnboardingService

    user_repo = PostgresUserRepository(repos.session_factory)
    provisioner = GreenProvisioner(
        client=green_client,
        credential_resolver=repos.connections,
    )
    webhook_base_url = os.environ.get(
        "ECHO_WEBHOOK_BASE_URL",
        "https://i-me.onrender.com",
    )
    onboarding_service = OnboardingService(
        bot=d360_client,
        user_repo=user_repo,
        connection_repo=repos.connections,
        provisioner=provisioner,
        green_client=green_client,
        webhook_base_url=webhook_base_url,
    )

    # --- scheduling service (executes due actions) ------------------------
    # Persistent idempotency store — survives restarts, prevents double-sends.
    # The Postgres implementation is built in compose.py as repos.idempotency.
    idempotency_store = repos.idempotency
    # --- snooze reminder validator --------------------------------------
    # Before sending a snooze reminder, check that the active item still
    # matches the state when the reminder was scheduled. This prevents
    # stale reminders from firing after the item was done, dismissed,
    # re-snoozed, or received a new message (version change).
    from echo_v2.services.feedback_service import make_snooze_reminder_validator

    snooze_validator = make_snooze_reminder_validator(repos.wfm_active)

    scheduling_service = SchedulingService(
        action_repo=repos.scheduled_actions,
        connection_repo=repos.connections,
        messaging=green_messaging,
        idempotency_store=idempotency_store,
        event_sink=LoggingEventSink(),
        bot_channel=d360_client,
        send_validator=snooze_validator,
    )

    # --- time parser (regex first, LLM fallback) --------------------------
    llm_parser = LLMTimeParser(
        client=openai_client,
        model=os.environ.get("LLM_MODEL_NAME", "gpt-4.1"),
    )
    time_parser = CombinedTimeParser(llm_parser=llm_parser)

    # --- scheduling flow service (3-step bot conversation) ----------------
    from echo_v2.persistence.contacts import PostgresContactRepository

    contact_repo = PostgresContactRepository(repos.session_factory)
    flow_service = SchedulingFlowService(
        bot=d360_client,
        state_repo=InMemoryConversationStateRepository(),
        scheduling_service=scheduling_service,
        time_parser=time_parser,
        user_resolver=_SessionUserResolver(repos.session_factory),
        contact_repo=contact_repo,
    )

    # --- digest reply service (handles "הצג הכול" button) -----------------
    from echo_v2.services.digest_reply import DigestReplyService

    digest_reply_service = DigestReplyService(
        bot=d360_client,
        active_repo=repos.wfm_active,
        chat_state_repo=repos.chat_state,
        message_repo=repos.messages,
        contact_repo=contact_repo,
        user_resolver=_SessionUserResolver(repos.session_factory),
    )

    # --- scheduler (background poller) -------------------------------------
    scheduler = Scheduler(
        service=scheduling_service,
        action_repo=repos.scheduled_actions,
        lease_seconds=float(os.environ.get("SCHEDULER_LEASE_SECONDS", "300")),
        poll_interval_seconds=float(os.environ.get("SCHEDULER_POLL_INTERVAL", "5")),
    )

    # --- chat ingestion (saves messages + manages analysis queue) ----------
    ingestion_service = ChatIngestionService(
        message_repo=repos.messages,
        chat_state_repo=repos.chat_state,
        quiet_period_seconds=float(os.environ.get("CHAT_QUIET_PERIOD_SECONDS", "300")),
        private_only=os.environ.get("CHAT_PRIVATE_ONLY", "true").lower()
        in ("1", "true", "yes"),
    )
    chat_dispatcher = ChatEventDispatcher(
        ingestion_service=ingestion_service,
        connection_repo=repos.connections,
        onboarding_service=onboarding_service,
    )

    # --- chat analysis worker (NOT started by default — CHAT_ANALYSIS_ENABLED)
    from echo_v2.services.waiting_for_me_analyzer import LLMWaitingForMeAnalyzer

    analyzer = LLMWaitingForMeAnalyzer(
        client=openai_client,
        model=os.environ.get("LLM_MODEL_NAME", "gpt-4.1"),
    )
    analysis_processor = ChatAnalysisProcessor(
        message_repo=repos.messages,
        analyzer=analyzer,
        context_messages=5,
        max_no_outbound=20,
    )
    analysis_worker = ChatAnalysisWorker(
        chat_state_repo=repos.chat_state,
        processor=analysis_processor,
        commit_repo=repos.analysis_commit,
        poll_interval_seconds=float(os.environ.get("CHAT_ANALYSIS_POLL_INTERVAL", "60")),
    )
    chat_analysis_enabled = os.environ.get("CHAT_ANALYSIS_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
    )

    # --- digest worker (NOT started by default — DIGEST_ENABLED) -------------
    from echo_v2.persistence.contacts import PostgresContactRepository
    from echo_v2.services.digest_worker import DigestWorker
    from echo_v2.services.waiting_list_query import WaitingListQueryService
    from echo_v2.services.waiting_list_token_service import WaitingListTokenService

    contact_repo = PostgresContactRepository(repos.session_factory)

    # --- waiting-list web app (token service + query service) ---------------
    token_service = WaitingListTokenService(
        repos.waiting_list_sessions,
        ttl_hours=int(os.environ.get("ECHO_WAITING_LIST_TOKEN_TTL_HOURS", "48")),
    )
    query_service = WaitingListQueryService(
        active_repo=repos.wfm_active,
        chat_state_repo=repos.chat_state,
        mute_repo=repos.chat_mutes,
    )

    async def user_provider():
        """Return all active users as (user_id, phone, timezone, first_name)."""
        from sqlalchemy import select

        from echo_v2.persistence.orm import UserRow

        async with repos.session_factory() as session:
            stmt = select(UserRow).where(UserRow.account_status == "active")
            rows = (await session.execute(stmt)).scalars().all()
            return [
                (str(r.id), r.phone_number, r.timezone, r.first_name)
                for r in rows
            ]

    digest_worker = DigestWorker(
        digest_repo=repos.daily_digests,
        active_repo=repos.wfm_active,
        chat_state_repo=repos.chat_state,
        message_repo=repos.messages,
        contact_repo=contact_repo,
        bot=d360_client,
        user_provider=user_provider,
        mute_repo=repos.chat_mutes,
        token_service=token_service,
        query_service=query_service,
        poll_interval_seconds=float(os.environ.get("DIGEST_POLL_INTERVAL", "300")),
        template_name=os.environ.get("DIGEST_TEMPLATE_NAME", "morning_waiting_digest5"),
    )
    digest_enabled = os.environ.get("DIGEST_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
    )

    # --- feedback flyloop (actions + feedback on waiting items) -------------
    from echo_v2.services.feedback_handler import FeedbackHandler
    from echo_v2.services.feedback_service import (
        WaitingForMeActionService,
        WaitingForMeFeedbackService,
    )

    # --- snooze reminder scheduling (via existing scheduler infra) ---------
    async def user_phone_lookup(user_id: str) -> str | None:
        from sqlalchemy import select

        from echo_v2.persistence.orm import UserRow

        async with repos.session_factory() as session:
            stmt = select(UserRow.phone_number).where(UserRow.id == user_id)
            result = await session.execute(stmt)
            row = result.first()
            return row[0] if row else None

    async def chat_name_lookup(user_id: str, chat_id: str) -> str | None:
        chat = await repos.chat_state.get(user_id, chat_id)
        return chat.chat_name if chat else None

    action_service = WaitingForMeActionService(
        active_repo=repos.wfm_active,
        action_repo=repos.wfm_actions,
        mute_repo=repos.chat_mutes,
        feedback_repo=repos.wfm_feedback,
        result_repo=repos.wfm_results,
        scheduling_service=scheduling_service,
        user_phone_lookup=user_phone_lookup,
        chat_name_lookup=chat_name_lookup,
    )
    feedback_service = WaitingForMeFeedbackService(
        feedback_repo=repos.wfm_feedback,
        result_repo=repos.wfm_results,
    )
    feedback_handler = FeedbackHandler(
        bot=d360_client,
        action_service=action_service,
        feedback_service=feedback_service,
        active_repo=repos.wfm_active,
        result_repo=repos.wfm_results,
        chat_state_repo=repos.chat_state,
        message_repo=repos.messages,
        contact_repo=contact_repo,
        mute_repo=repos.chat_mutes,
        user_resolver=flow_service._user_resolver,
        token_service=token_service,
        base_url=webhook_base_url,
    )

    # --- waiting-list mini web app (service + router) -----------------------
    from echo_v2.app.waiting_list_routes import build_waiting_list_router
    from echo_v2.services.waiting_list_service import WaitingListService

    bot_phone = os.environ.get("ECHO_BOT_PHONE", "972559937256")
    waiting_list_service = WaitingListService(
        token_service=token_service,
        query_service=query_service,
        action_service=action_service,
        action_repo=repos.wfm_actions,
        chat_state_repo=repos.chat_state,
        message_repo=repos.messages,
        contact_repo=contact_repo,
        result_repo=repos.wfm_results,
    )
    waiting_list_router = build_waiting_list_router(
        token_service=token_service,
        waiting_list_service=waiting_list_service,
        bot_phone=bot_phone,
    )

    # --- FastAPI app with lifespan (scheduler + worker start/stop with app) --
    scheduler_task: asyncio.Task | None = None
    analysis_worker_task: asyncio.Task | None = None
    digest_worker_task: asyncio.Task | None = None
    snooze_worker_task: asyncio.Task | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal scheduler_task, analysis_worker_task, digest_worker_task
        nonlocal snooze_worker_task
        # Startup: recover stale actions + start scheduler loop.
        try:
            recovered = await scheduler.recover()
            if recovered:
                _logger.info("recovered %d stale scheduled actions", recovered)
        except Exception:
            _logger.exception("scheduler recovery failed on startup")
        scheduler_task = asyncio.create_task(scheduler.run_loop())
        _logger.info("scheduler loop started")

        # Start chat analysis worker only if explicitly enabled.
        if chat_analysis_enabled:
            analysis_worker_task = asyncio.create_task(analysis_worker.run_loop())
            _logger.info("chat analysis worker loop started")

        # Start digest worker only if explicitly enabled.
        if digest_enabled:
            digest_worker_task = asyncio.create_task(digest_worker.run_loop())
            _logger.info("digest worker loop started")

        yield

        # Shutdown: cancel the loops.
        if digest_worker_task is not None:
            digest_worker_task.cancel()
            try:
                await digest_worker_task
            except asyncio.CancelledError:
                pass
            _logger.info("digest worker loop stopped")
        if analysis_worker_task is not None:
            analysis_worker_task.cancel()
            try:
                await analysis_worker_task
            except asyncio.CancelledError:
                pass
            _logger.info("chat analysis worker loop stopped")
        if scheduler_task is not None:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass
            _logger.info("scheduler loop stopped")

        # Close the shared OpenAI client.
        try:
            await raw_openai_client.close()
        except Exception:
            _logger.exception("error closing OpenAI client")

    app = FastAPI(title="Echo v2", version="0.1.0", lifespan=lifespan)

    # Green webhook: receives events from the user's WhatsApp (messages,
    # delivery status, connection state changes). Static URL — the instance
    # is resolved from the payload. Message events are deduped via the
    # messages table; status/state events via provider_webhook_events.
    green_router = build_green_router(
        connection_repo=repos.connections,
        dispatcher=chat_dispatcher,
        dedup_store=repos.webhooks,
    )
    app.include_router(green_router)

    # 360dialog webhook: receives messages from the Echo Business Bot
    # (user commands, vCards, time replies).
    dialog360_router = build_dialog360_router(
        flow_service=flow_service,
        webhook_secret=d360_settings.webhook_secret,
        adapter=Dialog360EventAdapter(),
        inbox=repos.bot_inbox,
        digest_reply_service=digest_reply_service,
        onboarding_service=onboarding_service,
        feedback_handler=feedback_handler,
    )
    app.include_router(dialog360_router)

    # Waiting-list mini web app: token→cookie exchange + JSON API.
    app.include_router(waiting_list_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


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
