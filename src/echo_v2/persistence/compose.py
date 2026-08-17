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
from echo_v2.persistence.postgres_idempotency import PostgresIdempotencyStore
from echo_v2.persistence.postgres_webhook_dedup import PostgresWebhookDedupStore
from echo_v2.persistence.postgres_whatsapp_connections import (
    PostgresWhatsAppConnectionRepository,
)
from echo_v2.persistence.settings import DBSettings
from echo_v2.persistence.unit_of_work import PostgresUnitOfWork

__all__ = ["PostgresRepos", "build_postgres_repos"]


@dataclass
class PostgresRepos:
    """Standalone Postgres repositories + a UoW factory."""

    connections: PostgresWhatsAppConnectionRepository
    webhooks: PostgresWebhookDedupStore
    idempotency: PostgresIdempotencyStore
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

    # A UoW factory bound to the same session_factory + cipher.
    class _BoundUoW(PostgresUnitOfWork):
        def __init__(self) -> None:
            super().__init__(factory, cipher)

    return PostgresRepos(
        connections=connections,
        webhooks=webhooks,
        idempotency=idempotency,
        session_factory=factory,
        unit_of_work=_BoundUoW,
    )
