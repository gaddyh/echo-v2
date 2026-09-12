"""Conftest for app tests — re-exports Postgres testcontainer fixtures.

The testcontainers Postgres fixtures live in tests/persistence/conftest.py.
Importing them here makes them available to every test under tests/app/.
"""

from __future__ import annotations

from tests.persistence.conftest import (  # noqa: F401
    clean_db,
    engine,
    insert_user,
    postgres_url,
    session_factory,
)
