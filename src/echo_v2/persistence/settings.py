"""Persistence-layer settings for Echo v2.

Loaded from environment variables. The application bootstrap is responsible
for calling :func:`load_dotenv` (python-dotenv) before constructing settings;
this module only reads ``os.getenv`` so it stays importable in test contexts
that inject values directly.

``DATABASE_URL`` uses the ``postgresql+psycopg://`` form for both sync
(Alembic) and async (app) — SQLAlchemy's psycopg dialect selects sync under
``create_engine()`` and async under ``create_async_engine()``. Do not strip
``+psycopg``.

``ECHO_CREDENTIAL_KEY`` is a 44-char url-safe base64 Fernet key used by
:class:`echo_v2.persistence.credential_cipher.LocalKeyCredentialCipher`.
Required in non-test environments; tests inject an
:class:`echo_v2.persistence.credential_cipher.IdentityCredentialCipher`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["DBSettings", "load_db_settings"]

_DEFAULT_POOL_SIZE = 5
_DEFAULT_PHONE_REGION = "IL"


@dataclass(frozen=True)
class DBSettings:
    """Configuration for the Postgres persistence layer."""

    database_url: str
    credential_key: bytes | None
    default_phone_region: str
    pool_size: int = _DEFAULT_POOL_SIZE
    echo: bool = False


def load_db_settings(
    *,
    database_url: str | None = None,
    credential_key: bytes | None = None,
    default_phone_region: str | None = None,
    pool_size: int | None = None,
    echo: bool | None = None,
) -> DBSettings:
    """Build :class:`DBSettings` from arguments or environment.

    ``DATABASE_URL`` is required — a missing URL is a deployment/configuration
    error, not a runtime error. ``ECHO_CREDENTIAL_KEY`` is optional here (the
    cipher layer raises if a real key is needed but absent); this keeps the
    settings loader usable in test contexts that inject an identity cipher.
    """

    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise ValueError(
            "DATABASE_URL is required. "
            "Set it in the environment (e.g. .env) before constructing DB components."
        )

    # Normalize: ensure +psycopg (psycopg3), not psycopg2.
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    raw_key = os.getenv("ECHO_CREDENTIAL_KEY") if credential_key is None else None
    parsed_key: bytes | None = credential_key
    if parsed_key is None and raw_key:
        parsed_key = raw_key.encode("utf-8")

    region = default_phone_region or os.getenv(
        "ECHO_DEFAULT_PHONE_REGION", _DEFAULT_PHONE_REGION
    )

    parsed_pool = pool_size if pool_size is not None else _DEFAULT_POOL_SIZE
    parsed_echo = echo if echo is not None else False

    return DBSettings(
        database_url=url,
        credential_key=parsed_key,
        default_phone_region=region,
        pool_size=parsed_pool,
        echo=parsed_echo,
    )
