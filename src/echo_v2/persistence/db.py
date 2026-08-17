"""Async engine, session factory, and migration runner for Echo v2.

The engine and session factory are constructed at app composition time and
injected into repositories — there is no module-level singleton (mirrors the
existing ``build_router`` factory style).

``DATABASE_URL`` uses the ``postgresql+psycopg://`` form for both sync and
async. ``create_async_engine()`` selects psycopg's async implementation;
``create_engine()`` (used by Alembic and admin scripts) selects sync. The
same URL works for both — do not strip ``+psycopg``.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from echo_v2.persistence.settings import DBSettings

__all__ = [
    "alembic_config_path",
    "async_session_factory",
    "create_async_engine_from_settings",
    "run_migrations",
]

_ALEMBIC_DIR = Path(__file__).parent / "alembic"
_ALEMBIC_INI = _ALEMBIC_DIR / "alembic.ini"


def alembic_config_path() -> Path:
    """Path to ``alembic.ini`` (lives next to this module)."""
    return _ALEMBIC_INI


def create_async_engine_from_settings(settings: DBSettings) -> AsyncEngine:
    """Build an :class:`AsyncEngine` from :class:`DBSettings`.

    ``pool_pre_ping=True`` so dead connections in the pool are detected
    before use (important for long-lived processes behind a DB that may
    restart).
    """
    return create_async_engine(
        settings.database_url,
        pool_size=settings.pool_size,
        pool_pre_ping=True,
        echo=settings.echo,
    )


def async_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Build an :func:`async_sessionmaker` bound to ``engine``."""
    return async_sessionmaker(engine, expire_on_commit=False)


def _sync_url(async_url: str) -> str:
    """No-op for psycopg: the same URL works for sync and async.

    Kept as a named function so the intent is explicit and the call site is
    self-documenting. SQLAlchemy's psycopg dialect selects sync under
    ``create_engine()`` and async under ``create_async_engine()``.
    """
    return async_url


def run_migrations(database_url: str) -> None:
    """Apply Alembic migrations to head, synchronously.

    Uses ``create_engine()`` (sync) against the same ``postgresql+psycopg://``
    URL. Intended for tests and programmatic bootstrap; production may prefer
    the ``alembic`` CLI.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_url(database_url))
    # The migrations directory is resolved relative to alembic.ini.
    command.upgrade(cfg, "head")


def create_sync_engine_for_migrations(database_url: str):
    """Build a sync engine for Alembic / admin scripts.

    Exposed for tests that need to inspect schema on a sync connection. The
    async app never uses this.
    """
    return create_engine(_sync_url(database_url))
