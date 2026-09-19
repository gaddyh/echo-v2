"""Tests for the transcription factory (build_transcriber)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from echo_v2.services.transcription_factory import build_transcriber


def test_build_transcriber_returns_none_when_not_configured():
    """When Modal settings are incomplete, build_transcriber returns None."""
    fake_settings = MagicMock()
    fake_settings.endpoint_url = None
    fake_settings.api_key = None
    fake_settings.api_secret = None

    with patch(
        "echo_v2.services.transcription_factory.load_settings",
        return_value=fake_settings,
    ), patch(
        "echo_v2.services.transcription_factory.is_configured",
        return_value=False,
    ):
        result = build_transcriber()
    assert result is None


def test_build_transcriber_builds_modal_when_configured():
    """When Modal settings are complete, build_transcriber returns a ModalWhisperTranscriber."""
    fake_settings = MagicMock()
    fake_settings.endpoint_url = "https://modal.example.com/transcribe"
    fake_settings.api_key = "key"
    fake_settings.api_secret = "secret"

    mock_transcriber = MagicMock()

    with patch(
        "echo_v2.services.transcription_factory.load_settings",
        return_value=fake_settings,
    ), patch(
        "echo_v2.services.transcription_factory.is_configured",
        return_value=True,
    ), patch(
        "echo_v2.integrations.modal.transcriber.ModalWhisperTranscriber",
        return_value=mock_transcriber,
    ):
        result = build_transcriber()
    assert result is mock_transcriber
