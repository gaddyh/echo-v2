"""Unit tests for db.py helpers (no Docker needed for most).

``run_migrations`` is tested against a real Postgres in the testcontainers
suite; here we cover the pure helpers that don't need a live DB.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from echo_v2.persistence.db import (
    _sync_url,
    alembic_config_path,
    async_session_factory,
    create_async_engine_from_settings,
    create_sync_engine_for_migrations,
)
from echo_v2.persistence.settings import DBSettings


def test_alembic_config_path_points_to_ini():
    path = alembic_config_path()
    assert path.name == "alembic.ini"
    assert path.exists()
    assert path.parent.name == "alembic"


def test_sync_url_is_identity_for_psycopg():
    """The same postgresql+psycopg:// URL works for sync and async — no stripping."""
    url = "postgresql+psycopg://echo:echo@localhost:5432/echo"
    assert _sync_url(url) == url


def test_sync_url_preserves_query_params():
    url = "postgresql+psycopg://u:p@h:5432/db?sslmode=require"
    assert _sync_url(url) == url


def test_create_async_engine_from_settings():
    settings = DBSettings(
        database_url="postgresql+psycopg://u:p@localhost:5432/db",
        credential_key=None,
        default_phone_region="IL",
        pool_size=3,
        echo=False,
    )
    engine = create_async_engine_from_settings(settings)
    assert isinstance(engine, AsyncEngine)
    # pool_size is passed through
    assert engine.pool.size() == 3
    # pool_pre_ping is enabled
    assert engine.pool._pre_ping is True  # type: ignore[attr-defined]


def test_create_async_engine_echo_flag():
    settings = DBSettings(
        database_url="postgresql+psycopg://u:p@localhost:5432/db",
        credential_key=None,
        default_phone_region="IL",
        echo=True,
    )
    engine = create_async_engine_from_settings(settings)
    assert engine.echo is True


def test_async_session_factory_returns_sessionmaker():
    # Use a psycopg URL — engine is created but never connects.
    engine = create_async_engine("postgresql+psycopg://u:p@localhost:5432/db")
    factory = async_session_factory(engine)
    assert isinstance(factory, async_sessionmaker)


def test_create_sync_engine_for_migrations():
    """The sync engine uses the same psycopg URL (not stripped)."""
    engine = create_sync_engine_for_migrations("postgresql+psycopg://u:p@localhost:5432/db")
    assert engine is not None
    # The URL should still contain +psycopg
    assert "+psycopg" in str(engine.url)
    engine.dispose()
