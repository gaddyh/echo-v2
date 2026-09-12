"""Composition helpers for the Postgres persistence layer.

Builds standalone repositories and a :class:`PostgresUnitOfWork` factory
from :class:`DBSettings`, for use by the future app bootstrap. Not wired
into FastAPI in this milestone — the module-level ``green_webhook_router``
singleton stays in-memory so tests without Docker keep working.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from echo_v2.persistence.credential_cipher import (
    CredentialCipher,
    IdentityCredentialCipher,
    LocalKeyCredentialCipher,
)
from echo_v2.persistence.db import (
    async_session_factory,
    create_async_engine_from_settings,
)
from echo_v2.persistence.postgres_chat import (
    PostgresAnalysisCommitRepository,
    PostgresChatStateRepository,
    PostgresMessageRepository,
    PostgresWaitingForMeActiveRepository,
    PostgresWaitingForMeResultRepository,
)
from echo_v2.persistence.postgres_digest import PostgresDailyDigestRepository
from echo_v2.persistence.postgres_feedback import (
    PostgresChatMuteRepository,
    PostgresWaitingForMeActionRepository,
    PostgresWaitingForMeFeedbackRepository,
)
from echo_v2.persistence.postgres_idempotency import PostgresIdempotencyStore
from echo_v2.persistence.postgres_scheduled_actions import (
    PostgresScheduledActionRepository,
)
from echo_v2.persistence.postgres_webhook_dedup import PostgresWebhookDedupStore
from echo_v2.persistence.postgres_whatsapp_connections import (
    PostgresWhatsAppConnectionRepository,
)
from echo_v2.persistence.settings import DBSettings
from echo_v2.persistence.unit_of_work import PostgresUnitOfWork
from echo_v2.persistence.waiting_list_tokens import (
    PostgresWaitingListSessionRepository,
)

__all__ = ["PostgresRepos", "build_postgres_repos"]


@dataclass
class PostgresRepos:
    """Standalone Postgres repositories + a UoW factory."""

    connections: PostgresWhatsAppConnectionRepository
    webhooks: PostgresWebhookDedupStore
    idempotency: PostgresIdempotencyStore
    scheduled_actions: PostgresScheduledActionRepository
    messages: PostgresMessageRepository
    chat_state: PostgresChatStateRepository
    wfm_results: PostgresWaitingForMeResultRepository
    wfm_active: PostgresWaitingForMeActiveRepository
    analysis_commit: PostgresAnalysisCommitRepository
    wfm_feedback: PostgresWaitingForMeFeedbackRepository
    wfm_actions: PostgresWaitingForMeActionRepository
    chat_mutes: PostgresChatMuteRepository
    daily_digests: PostgresDailyDigestRepository
    waiting_list_sessions: PostgresWaitingListSessionRepository
    session_factory: async_sessionmaker
    unit_of_work: type[PostgresUnitOfWork]


def build_postgres_repos(settings: DBSettings) -> PostgresRepos:
    """Build standalone Postgres repos + UoW factory from settings.

    The cipher is :class:`LocalKeyCredentialCipher` if ``settings.credential_key``
    is set, else :class:`IdentityCredentialCipher` (test/dev only — production
    must set ``ECHO_CREDENTIAL_KEY``).
    """
    engine = create_async_engine_from_settings(settings)
    factory = async_session_factory(engine)

    cipher: CredentialCipher
    if settings.credential_key is not None:
        cipher = LocalKeyCredentialCipher(settings.credential_key)
    else:
        cipher = IdentityCredentialCipher()

    connections = PostgresWhatsAppConnectionRepository(factory, cipher)
    webhooks = PostgresWebhookDedupStore(factory)
    idempotency = PostgresIdempotencyStore(factory)
    scheduled_actions = PostgresScheduledActionRepository(factory)
    messages = PostgresMessageRepository(factory)
    chat_state = PostgresChatStateRepository(factory)
    wfm_results = PostgresWaitingForMeResultRepository(factory)
    wfm_active = PostgresWaitingForMeActiveRepository(factory)
    analysis_commit = PostgresAnalysisCommitRepository(factory)
    wfm_feedback = PostgresWaitingForMeFeedbackRepository(factory)
    wfm_actions = PostgresWaitingForMeActionRepository(factory)
    chat_mutes = PostgresChatMuteRepository(factory)
    daily_digests = PostgresDailyDigestRepository(factory)
    waiting_list_sessions = PostgresWaitingListSessionRepository(factory)

    # A UoW factory bound to the same session_factory + cipher.
    class _BoundUoW(PostgresUnitOfWork):
        def __init__(self) -> None:
            super().__init__(factory, cipher)

    return PostgresRepos(
        connections=connections,
        webhooks=webhooks,
        idempotency=idempotency,
        scheduled_actions=scheduled_actions,
        messages=messages,
        chat_state=chat_state,
        wfm_results=wfm_results,
        wfm_active=wfm_active,
        analysis_commit=analysis_commit,
        wfm_feedback=wfm_feedback,
        wfm_actions=wfm_actions,
        chat_mutes=chat_mutes,
        daily_digests=daily_digests,
        waiting_list_sessions=waiting_list_sessions,
        session_factory=factory,
        unit_of_work=_BoundUoW,
    )
