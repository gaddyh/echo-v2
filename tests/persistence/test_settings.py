"""Unit tests for DBSettings and load_db_settings (no Docker needed)."""

from __future__ import annotations

import pytest

from echo_v2.persistence.settings import DBSettings, load_db_settings


def test_load_db_settings_from_explicit_args():
    settings = load_db_settings(
        database_url="postgresql+psycopg://u:p@localhost:5432/db",
        credential_key=b"test-key",
        default_phone_region="US",
        pool_size=10,
        echo=True,
    )
    assert settings.database_url == "postgresql+psycopg://u:p@localhost:5432/db"
    assert settings.credential_key == b"test-key"
    assert settings.default_phone_region == "US"
    assert settings.pool_size == 10
    assert settings.echo is True


def test_load_db_settings_defaults():
    settings = load_db_settings(database_url="postgresql+psycopg://u:p@localhost/db")
    assert settings.credential_key is None
    assert settings.default_phone_region == "IL"
    assert settings.pool_size == 5
    assert settings.echo is False


def test_load_db_settings_missing_url_raises_value_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        load_db_settings()


def test_load_db_settings_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://env:env@localhost/envdb")
    monkeypatch.setenv("ECHO_CREDENTIAL_KEY", "env-key-bytes")
    monkeypatch.setenv("ECHO_DEFAULT_PHONE_REGION", "US")
    settings = load_db_settings()
    assert settings.database_url == "postgresql+psycopg://env:env@localhost/envdb"
    assert settings.credential_key == b"env-key-bytes"
    assert settings.default_phone_region == "US"


def test_load_db_settings_explicit_url_overrides_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://env@localhost/envdb")
    settings = load_db_settings(database_url="postgresql+psycopg://explicit@localhost/mydb")
    assert settings.database_url == "postgresql+psycopg://explicit@localhost/mydb"


def test_load_db_settings_explicit_credential_key_overrides_env(monkeypatch):
    monkeypatch.setenv("ECHO_CREDENTIAL_KEY", "env-key")
    settings = load_db_settings(
        database_url="postgresql+psycopg://u:p@localhost/db",
        credential_key=b"explicit-key",
    )
    assert settings.credential_key == b"explicit-key"


def test_load_db_settings_no_credential_key_when_env_absent(monkeypatch):
    monkeypatch.delenv("ECHO_CREDENTIAL_KEY", raising=False)
    settings = load_db_settings(database_url="postgresql+psycopg://u:p@localhost/db")
    assert settings.credential_key is None


def test_load_db_settings_empty_url_raises(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        load_db_settings(database_url="")


def test_db_settings_is_frozen():
    from dataclasses import FrozenInstanceError

    settings = DBSettings(
        database_url="postgresql+psycopg://u:p@localhost/db",
        credential_key=None,
        default_phone_region="IL",
    )
    with pytest.raises(FrozenInstanceError):
        settings.database_url = "other"  # type: ignore[misc]
