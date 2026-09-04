"""Alembic migration environment for Echo v2.

Reads ``DATABASE_URL`` from the environment (falling back to the
``sqlalchemy.url`` in ``alembic.ini``). Uses the same
``postgresql+psycopg://`` URL for sync (Alembic) and async (app) —
SQLAlchemy's psycopg dialect selects sync under ``create_engine()``.

The ORM metadata is imported so ``alembic revision --autogenerate`` can
diff against the models. The migration DDL is the source of truth for
production schema; autogenerate is a convenience, not a guarantee.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from dotenv import load_dotenv

from alembic import context
from sqlalchemy import engine_from_config, pool

from echo_v2.persistence.orm import Base

# Load .env so DATABASE_URL is available without manual export.
load_dotenv()

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the URL from env if present (programmatic / testcontainers use).
_env_url = os.getenv("DATABASE_URL")
if _env_url:
    config.set_main_option("sqlalchemy.url", _env_url)

# Echo ORM metadata — enables `alembic revision --autogenerate`.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (create a sync engine and connect)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
