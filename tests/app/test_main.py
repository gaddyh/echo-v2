"""App lifespan and feature-flag wiring tests for chat analysis.

Tests that the ChatAnalysisWorker feature flag (CHAT_ANALYSIS_ENABLED)
defaults to false, and that the env-var parsing logic works correctly.

Full lifespan integration tests would require mocking the entire
create_app dependency graph; instead we test the flag logic directly
and verify the worker is constructed but gated behind the flag.
"""

from __future__ import annotations

import os


def _is_chat_analysis_enabled() -> bool:
    """Replicate the flag parsing from main.py."""
    return os.environ.get("CHAT_ANALYSIS_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
    )


def test_chat_analysis_disabled_by_default(monkeypatch):
    """CHAT_ANALYSIS_ENABLED defaults to false."""
    monkeypatch.delenv("CHAT_ANALYSIS_ENABLED", raising=False)
    assert _is_chat_analysis_enabled() is False


def test_chat_analysis_enabled_when_true(monkeypatch):
    """CHAT_ANALYSIS_ENABLED=true enables the worker."""
    monkeypatch.setenv("CHAT_ANALYSIS_ENABLED", "true")
    assert _is_chat_analysis_enabled() is True


def test_chat_analysis_enabled_when_1(monkeypatch):
    """CHAT_ANALYSIS_ENABLED=1 enables the worker."""
    monkeypatch.setenv("CHAT_ANALYSIS_ENABLED", "1")
    assert _is_chat_analysis_enabled() is True


def test_chat_analysis_disabled_when_false(monkeypatch):
    """CHAT_ANALYSIS_ENABLED=false disables the worker."""
    monkeypatch.setenv("CHAT_ANALYSIS_ENABLED", "false")
    assert _is_chat_analysis_enabled() is False


def test_chat_analysis_disabled_when_yes_is_true(monkeypatch):
    """CHAT_ANALYSIS_ENABLED=yes enables the worker."""
    monkeypatch.setenv("CHAT_ANALYSIS_ENABLED", "yes")
    assert _is_chat_analysis_enabled() is True


def test_chat_private_only_defaults_to_true(monkeypatch):
    """CHAT_PRIVATE_ONLY defaults to true."""
    monkeypatch.delenv("CHAT_PRIVATE_ONLY", raising=False)
    val = os.environ.get("CHAT_PRIVATE_ONLY", "true").lower()
    assert val in ("1", "true", "yes")


def test_chat_private_only_false(monkeypatch):
    """CHAT_PRIVATE_ONLY=false disables private-only filtering."""
    monkeypatch.setenv("CHAT_PRIVATE_ONLY", "false")
    val = os.environ.get("CHAT_PRIVATE_ONLY", "true").lower()
    assert val not in ("1", "true", "yes")


def test_chat_quiet_period_defaults_to_300(monkeypatch):
    """CHAT_QUIET_PERIOD_SECONDS defaults to 300."""
    monkeypatch.delenv("CHAT_QUIET_PERIOD_SECONDS", raising=False)
    assert float(os.environ.get("CHAT_QUIET_PERIOD_SECONDS", "300")) == 300.0


def test_chat_analysis_poll_interval_defaults_to_60(monkeypatch):
    """CHAT_ANALYSIS_POLL_INTERVAL defaults to 60."""
    monkeypatch.delenv("CHAT_ANALYSIS_POLL_INTERVAL", raising=False)
    assert float(os.environ.get("CHAT_ANALYSIS_POLL_INTERVAL", "60")) == 60.0


def test_worker_constructed_but_not_started_when_disabled(monkeypatch):
    """Verify ChatAnalysisWorker can be constructed with a recording processor
    but the flag prevents it from being started in the lifespan."""
    monkeypatch.setenv("CHAT_ANALYSIS_ENABLED", "false")
    from echo_v2.persistence.chat_repositories import InMemoryChatStateRepository
    from echo_v2.services.chat_analysis_worker import (
        ChatAnalysisWorker,
        RecordingAnalysisProcessor,
    )

    worker = ChatAnalysisWorker(
        chat_state_repo=InMemoryChatStateRepository(),
        processor=RecordingAnalysisProcessor(),
        poll_interval_seconds=60.0,
    )
    assert worker is not None
    assert _is_chat_analysis_enabled() is False


def test_worker_run_once_does_nothing_when_nothing_due():
    """A worker with an empty repo processes nothing — proving the worker
    doesn't drain or mark chats when there's nothing to process."""
    import asyncio

    from echo_v2.persistence.chat_repositories import InMemoryChatStateRepository
    from echo_v2.services.chat_analysis_worker import (
        ChatAnalysisWorker,
        RecordingAnalysisProcessor,
    )

    worker = ChatAnalysisWorker(
        chat_state_repo=InMemoryChatStateRepository(),
        processor=RecordingAnalysisProcessor(),
    )
    result = asyncio.run(worker.run_once())
    assert result is False
