"""Unit tests for compose.py — wiring of Postgres repos from settings.

These tests verify the composition logic (which cipher is selected, which
repos are built, UoW factory works) without needing a live Postgres. The
engine is created but never connected — we only inspect the wiring.
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from echo_v2.persistence.compose import PostgresRepos, build_postgres_repos
from echo_v2.persistence.credential_cipher import (
    IdentityCredentialCipher,
    LocalKeyCredentialCipher,
)
from echo_v2.persistence.postgres_idempotency import PostgresIdempotencyStore
from echo_v2.persistence.postgres_webhook_dedup import PostgresWebhookDedupStore
from echo_v2.persistence.postgres_whatsapp_connections import (
    PostgresWhatsAppConnectionRepository,
)
from echo_v2.persistence.settings import DBSettings
from echo_v2.persistence.unit_of_work import PostgresUnitOfWork


def _settings(*, credential_key: bytes | None = None) -> DBSettings:
    return DBSettings(
        database_url="postgresql+psycopg://u:p@localhost:5432/db",
        credential_key=credential_key,
        default_phone_region="IL",
        pool_size=5,
        echo=False,
    )


def test_build_postgres_repos_returns_postgres_repos():
    repos = build_postgres_repos(_settings())
    assert isinstance(repos, PostgresRepos)


def test_build_postgres_repos_builds_all_three_repos():
    repos = build_postgres_repos(_settings())
    assert isinstance(repos.connections, PostgresWhatsAppConnectionRepository)
    assert isinstance(repos.webhooks, PostgresWebhookDedupStore)
    assert isinstance(repos.idempotency, PostgresIdempotencyStore)


def test_build_postgres_repos_session_factory_is_set():
    repos = build_postgres_repos(_settings())
    assert repos.session_factory is not None


def test_build_postgres_repos_uow_factory_is_subclass():
    repos = build_postgres_repos(_settings())
    assert issubclass(repos.unit_of_work, PostgresUnitOfWork)


def test_build_postgres_repos_uow_factory_constructs():
    repos = build_postgres_repos(_settings())
    uow = repos.unit_of_work()
    assert isinstance(uow, PostgresUnitOfWork)
    # The UoW's cipher should match the settings
    assert uow._cipher is not None  # type: ignore[attr-defined]


def test_build_postgres_repos_uses_identity_cipher_when_no_key():
    repos = build_postgres_repos(_settings(credential_key=None))
    assert isinstance(repos.connections._cipher, IdentityCredentialCipher)  # type: ignore[attr-defined]


def test_build_postgres_repos_uses_local_key_cipher_when_key_set():
    key = Fernet.generate_key()
    repos = build_postgres_repos(_settings(credential_key=key))
    assert isinstance(repos.connections._cipher, LocalKeyCredentialCipher)  # type: ignore[attr-defined]


def test_build_postgres_repos_repos_share_session_factory():
    repos = build_postgres_repos(_settings())
    # All three repos should share the same session factory
    assert repos.connections._session_factory is repos.webhooks._session_factory  # type: ignore[attr-defined]
    assert repos.webhooks._session_factory is repos.idempotency._session_factory  # type: ignore[attr-defined]
