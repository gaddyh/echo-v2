from __future__ import annotations

import pytest

from echo_v2.integrations.baileys.settings import load_settings


def test_load_settings_uses_explicit_values():
    settings = load_settings(
        connector_url="http://localhost:8080/",
        internal_api_token="token",
    )
    assert settings.connector_url == "http://localhost:8080"
    assert settings.internal_api_token == "token"


def test_load_settings_reads_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BAILEYS_CONNECTOR_URL", "http://connector/")
    monkeypatch.setenv("BAILEYS_CONNECTOR_TOKEN", "env-token")
    settings = load_settings()
    assert settings.connector_url == "http://connector"
    assert settings.internal_api_token == "env-token"


def test_load_settings_requires_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BAILEYS_CONNECTOR_TOKEN", raising=False)
    with pytest.raises(ValueError, match="BAILEYS_CONNECTOR_TOKEN"):
        load_settings(connector_url="http://connector")
