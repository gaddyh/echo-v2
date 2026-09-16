"""Tests for the BotFlowRegistry.

Covers the onboarding and scheduling flow detection branches in
``active_flow_for`` — the router delegates to the registry when no
explicit command matched.
"""

from __future__ import annotations

import pytest

from echo_v2.bot.flow_registry import BotFlowRegistry
from echo_v2.domain.conversation import SchedulingFlowContext, SchedulingFlowState
from echo_v2.ports.bot import BotEvent, BotEventType

pytestmark = pytest.mark.asyncio


def _text_event(text: str = "hello") -> BotEvent:
    return BotEvent(
        type=BotEventType.TEXT,
        user_phone="972501234567",
        text=text,
        event_id="evt-1",
    )


def _button_event() -> BotEvent:
    return BotEvent(
        type=BotEventType.BUTTON_REPLY,
        user_phone="972501234567",
        button_id="action:active-1:handled",
        event_id="evt-1",
    )


class _FakeOnboarding:
    """Fake onboarding service for flow registry tests."""

    def __init__(self, *, is_onboarding: bool = False, handled: bool = True):
        self._is_onboarding = is_onboarding
        self._handled = handled
        self.name_responses: list[tuple[str, str]] = []

    async def is_onboarding(self, phone: str) -> bool:
        return self._is_onboarding

    async def handle_name_response(self, phone: str, text: str) -> bool:
        self.name_responses.append((phone, text))
        return self._handled


class _FakeStateRepo:
    """Fake scheduling state repo — returns a fixed context."""

    def __init__(self, ctx: SchedulingFlowContext | None):
        self._ctx = ctx
        self.get_calls: list[str] = []

    async def get(self, user_id: str) -> SchedulingFlowContext | None:
        self.get_calls.append(user_id)
        return self._ctx


class _FakeSchedulingFlow:
    """Fake scheduling flow service."""

    def __init__(self, state_repo: _FakeStateRepo):
        self._state_repo = state_repo
        self.handled_events: list[BotEvent] = []

    async def handle(self, event: BotEvent) -> None:
        self.handled_events.append(event)


# --- Onboarding flow --------------------------------------------------------


async def test_onboarding_text_handled_returns_true():
    """Onboarding user sends text, name response handled → True."""
    onboarding = _FakeOnboarding(is_onboarding=True, handled=True)
    state_repo = _FakeStateRepo(None)
    scheduling = _FakeSchedulingFlow(state_repo)
    registry = BotFlowRegistry(
        onboarding_service=onboarding, scheduling_flow=scheduling,
    )
    event = _text_event("Gaddy")
    result = await registry.active_flow_for(event, user_id="user-1")
    assert result is True
    assert onboarding.name_responses == [("972501234567", "Gaddy")]
    # Scheduling flow should NOT be checked when onboarding is active.
    assert state_repo.get_calls == []


async def test_onboarding_text_not_handled_still_returns_true():
    """Onboarding user, name response NOT handled → still True (stop)."""
    onboarding = _FakeOnboarding(is_onboarding=True, handled=False)
    state_repo = _FakeStateRepo(None)
    scheduling = _FakeSchedulingFlow(state_repo)
    registry = BotFlowRegistry(
        onboarding_service=onboarding, scheduling_flow=scheduling,
    )
    event = _text_event("whatever")
    result = await registry.active_flow_for(event, user_id="user-1")
    assert result is True
    assert onboarding.name_responses == [("972501234567", "whatever")]
    assert state_repo.get_calls == []


async def test_onboarding_non_text_event_returns_true():
    """Onboarding user sends a non-TEXT event → True without calling
    handle_name_response."""
    onboarding = _FakeOnboarding(is_onboarding=True, handled=True)
    state_repo = _FakeStateRepo(None)
    scheduling = _FakeSchedulingFlow(state_repo)
    registry = BotFlowRegistry(
        onboarding_service=onboarding, scheduling_flow=scheduling,
    )
    event = _button_event()
    result = await registry.active_flow_for(event, user_id="user-1")
    assert result is True
    assert onboarding.name_responses == []
    assert state_repo.get_calls == []


async def test_onboarding_text_empty_returns_true():
    """Onboarding user sends TEXT with empty text → True without calling
    handle_name_response."""
    onboarding = _FakeOnboarding(is_onboarding=True, handled=True)
    state_repo = _FakeStateRepo(None)
    scheduling = _FakeSchedulingFlow(state_repo)
    registry = BotFlowRegistry(
        onboarding_service=onboarding, scheduling_flow=scheduling,
    )
    event = _text_event("")
    result = await registry.active_flow_for(event, user_id="user-1")
    assert result is True
    assert onboarding.name_responses == []
    assert state_repo.get_calls == []


# --- Scheduling flow --------------------------------------------------------


async def test_scheduling_active_context_handles_event():
    """No onboarding; scheduling context is AWAITING_MESSAGE → handle + True."""
    onboarding = _FakeOnboarding(is_onboarding=False)
    ctx = SchedulingFlowContext(
        user_id="user-1", state=SchedulingFlowState.AWAITING_MESSAGE,
    )
    state_repo = _FakeStateRepo(ctx)
    scheduling = _FakeSchedulingFlow(state_repo)
    registry = BotFlowRegistry(
        onboarding_service=onboarding, scheduling_flow=scheduling,
    )
    event = _text_event("schedule this")
    result = await registry.active_flow_for(event, user_id="user-1")
    assert result is True
    assert scheduling.handled_events == [event]
    assert state_repo.get_calls == ["user-1"]


async def test_scheduling_idle_context_returns_false():
    """No onboarding; scheduling context is IDLE → False (no active flow)."""
    onboarding = _FakeOnboarding(is_onboarding=False)
    ctx = SchedulingFlowContext(
        user_id="user-1", state=SchedulingFlowState.IDLE,
    )
    state_repo = _FakeStateRepo(ctx)
    scheduling = _FakeSchedulingFlow(state_repo)
    registry = BotFlowRegistry(
        onboarding_service=onboarding, scheduling_flow=scheduling,
    )
    event = _text_event("hello")
    result = await registry.active_flow_for(event, user_id="user-1")
    assert result is False
    assert scheduling.handled_events == []


async def test_no_scheduling_context_returns_false():
    """No onboarding; no scheduling context (None) → False."""
    onboarding = _FakeOnboarding(is_onboarding=False)
    state_repo = _FakeStateRepo(None)
    scheduling = _FakeSchedulingFlow(state_repo)
    registry = BotFlowRegistry(
        onboarding_service=onboarding, scheduling_flow=scheduling,
    )
    event = _text_event("hello")
    result = await registry.active_flow_for(event, user_id="user-1")
    assert result is False
    assert scheduling.handled_events == []
