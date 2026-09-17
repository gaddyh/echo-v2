"""Tests for the BotCommandRouter and BotCommandParser.

Covers:
* Parser: callback IDs and text keywords → typed commands.
* Router: resolve actor → command → flow → fallback priority.
* Flow registry: onboarding and scheduling flow detection.
"""

from __future__ import annotations

import pytest

from echo_v2.bot import (
    BotCommandParser,
    BotCommandRouter,
    Cancel,
    DigestOpen,
    ListDone,
    OnboardingCode,
    OnboardingInfo,
    OnboardingQr,
    OnboardingStart,
    ResponsibilityDismiss,
    ResponsibilityDone,
    ResponsibilityList,
    ResponsibilitySnooze,
)
from echo_v2.ports.bot import BotEvent, BotEventType

pytestmark = pytest.mark.asyncio


def _text_event(
    text: str,
    *,
    user_phone: str = "972501234567",
) -> BotEvent:
    return BotEvent(
        type=BotEventType.TEXT,
        user_phone=user_phone,
        text=text,
        event_id="evt-1",
    )


def _button_event(
    button_id: str,
    *,
    user_phone: str = "972501234567",
) -> BotEvent:
    return BotEvent(
        type=BotEventType.BUTTON_REPLY,
        user_phone=user_phone,
        button_id=button_id,
        event_id="evt-1",
    )


# --- Parser: callback IDs ---------------------------------------------------


def test_parse_action_handled():
    parser = BotCommandParser()
    cmd = parser.parse(_button_event("action:active-1:handled"))
    assert isinstance(cmd, ResponsibilityDone)
    assert cmd.responsibility_id == "active-1"


def test_parse_action_snooze():
    parser = BotCommandParser()
    cmd = parser.parse(_button_event("action:active-1:snooze"))
    assert isinstance(cmd, ResponsibilitySnooze)
    assert cmd.responsibility_id == "active-1"
    assert cmd.preset is None


def test_parse_action_snooze_with_preset():
    parser = BotCommandParser()
    cmd = parser.parse(_button_event("action:active-1:snooze:1h"))
    assert isinstance(cmd, ResponsibilitySnooze)
    assert cmd.responsibility_id == "active-1"
    assert cmd.preset == "1h"


def test_parse_action_dismiss():
    parser = BotCommandParser()
    cmd = parser.parse(_button_event("action:active-1:dismiss"))
    assert isinstance(cmd, ResponsibilityDismiss)
    assert cmd.responsibility_id == "active-1"
    assert cmd.reason is None


def test_parse_dismiss_with_reason():
    parser = BotCommandParser()
    cmd = parser.parse(_button_event("dismiss:active-1:not_waiting"))
    assert isinstance(cmd, ResponsibilityDismiss)
    assert cmd.responsibility_id == "active-1"
    assert cmd.reason == "not_waiting"


def test_parse_onboarding_start():
    parser = BotCommandParser()
    cmd = parser.parse(_button_event("onboarding:start"))
    assert isinstance(cmd, OnboardingStart)


def test_parse_onboarding_info():
    parser = BotCommandParser()
    cmd = parser.parse(_button_event("onboarding:info"))
    assert isinstance(cmd, OnboardingInfo)


def test_parse_unknown_callback():
    parser = BotCommandParser()
    cmd = parser.parse(_button_event("unknown:foo:bar"))
    assert cmd is None


# --- Parser: text keywords --------------------------------------------------


def test_parse_text_code():
    parser = BotCommandParser()
    cmd = parser.parse(_text_event("קוד"))
    assert isinstance(cmd, OnboardingCode)


def test_parse_text_qr():
    parser = BotCommandParser()
    cmd = parser.parse(_text_event("qr"))
    assert isinstance(cmd, OnboardingQr)


def test_parse_text_digest():
    parser = BotCommandParser()
    cmd = parser.parse(_text_event("סיכום חדש"))
    assert isinstance(cmd, DigestOpen)


def test_parse_text_digest_with_please():
    parser = BotCommandParser()
    cmd = parser.parse(_text_event("סיכום חדש בבקשה"))
    assert isinstance(cmd, DigestOpen)


def test_parse_text_view_details():
    parser = BotCommandParser()
    cmd = parser.parse(_text_event("צפה בשיחות"))
    assert isinstance(cmd, ResponsibilityList)


def test_parse_text_view_details_with_brackets():
    parser = BotCommandParser()
    cmd = parser.parse(_text_event("[צפה בשיחות]"))
    assert isinstance(cmd, ResponsibilityList)


def test_parse_text_list_done():
    parser = BotCommandParser()
    cmd = parser.parse(_text_event("סיימתי לעבור על רשימת ההמתנה"))
    assert isinstance(cmd, ListDone)


def test_parse_text_consent_phrase():
    parser = BotCommandParser()
    cmd = parser.parse(_text_event("חברו אותי"))
    assert isinstance(cmd, OnboardingStart)


def test_parse_text_cancel_hebrew():
    parser = BotCommandParser()
    cmd = parser.parse(_text_event("בטל"))
    assert isinstance(cmd, Cancel)


def test_parse_text_cancel_english():
    parser = BotCommandParser()
    cmd = parser.parse(_text_event("cancel"))
    assert isinstance(cmd, Cancel)


def test_parse_text_unknown():
    parser = BotCommandParser()
    cmd = parser.parse(_text_event("hello world"))
    assert cmd is None


def test_parse_non_text_non_button():
    parser = BotCommandParser()
    event = BotEvent(
        type=BotEventType.CONTACT,
        user_phone="972501234567",
        event_id="evt-1",
    )
    cmd = parser.parse(event)
    assert cmd is None


# --- Router: dispatch priority ---------------------------------------------


class _FakeUserResolver:
    def __init__(self, user_id: str | None = "user-1"):
        self._user_id = user_id

    async def resolve(self, phone: str):
        if self._user_id is None:
            return None
        return (self._user_id, "UTC")


class _FakeHandlers:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle_responsibility_done(
        self, event: BotEvent, active_id: str,
    ) -> None:
        self.calls.append(f"done:{active_id}")

    async def handle_responsibility_snooze(
        self, event: BotEvent, active_id: str, preset: str | None,
    ) -> None:
        self.calls.append(f"snooze:{active_id}:{preset}")

    async def handle_responsibility_dismiss(
        self, event: BotEvent, active_id: str, reason: str | None,
    ) -> None:
        self.calls.append(f"dismiss:{active_id}:{reason}")

    async def handle_responsibility_list(self, event: BotEvent) -> None:
        self.calls.append("list")

    async def handle_digest_open(self, event: BotEvent) -> None:
        self.calls.append("digest")

    async def handle_list_done(self, event: BotEvent) -> None:
        self.calls.append("list_done")

    async def handle_onboarding_code(self, phone: str) -> bool:
        self.calls.append(f"code:{phone}")
        return True

    async def handle_onboarding_qr(self, phone: str) -> bool:
        self.calls.append(f"qr:{phone}")
        return True

    async def handle_onboarding_start(self, phone: str) -> None:
        self.calls.append(f"start:{phone}")

    async def handle_onboarding_info(self, phone: str) -> None:
        self.calls.append(f"info:{phone}")


class _FakeOnboardingEntry:
    def __init__(self) -> None:
        self.unknown_events: list[BotEvent] = []

    async def handle_unknown_event(self, event: BotEvent) -> None:
        self.unknown_events.append(event)


class _FakeFlowRegistry:
    def __init__(self, *, has_flow: bool = False) -> None:
        self.has_flow = has_flow
        self.flow_events: list[BotEvent] = []

    async def active_flow_for(
        self, event: BotEvent, user_id: str,
    ) -> bool:
        if self.has_flow:
            self.flow_events.append(event)
            return True
        return False


class _FakeFallback:
    def __init__(self) -> None:
        self.events: list[BotEvent] = []

    async def __call__(self, event: BotEvent) -> None:
        self.events.append(event)


async def test_router_unknown_user_routes_to_onboarding():
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(user_id=None),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
    )
    event = _text_event("hello")
    await router.route(event)
    assert onboarding.unknown_events == [event]
    assert handlers.calls == []
    assert fallback.events == []


async def test_router_known_user_command_dispatches():
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
    )
    await router.route(_button_event("action:active-1:handled"))
    assert handlers.calls == ["done:active-1"]
    assert fallback.events == []


async def test_router_no_command_no_flow_falls_through():
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    flow_registry = _FakeFlowRegistry(has_flow=False)
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
        flow_registry=flow_registry,
    )
    event = _text_event("hello world")
    await router.route(event)
    assert handlers.calls == []
    assert fallback.events == [event]


async def test_router_no_command_active_flow_routes_to_flow():
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    flow_registry = _FakeFlowRegistry(has_flow=True)
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
        flow_registry=flow_registry,
    )
    event = _text_event("some message")
    await router.route(event)
    assert handlers.calls == []
    assert fallback.events == []
    assert flow_registry.flow_events == [event]


async def test_router_command_wins_over_flow():
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    flow_registry = _FakeFlowRegistry(has_flow=True)
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
        flow_registry=flow_registry,
    )
    # Even with an active flow, an explicit command should win.
    await router.route(_button_event("action:active-1:handled"))
    assert handlers.calls == ["done:active-1"]
    assert flow_registry.flow_events == []


async def test_router_cancel_passes_to_fallback():
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
    )
    await router.route(_text_event("בטל"))
    assert handlers.calls == []
    assert fallback.events != []


# --- Router: no flow_registry falls through to fallback -------------------


async def test_router_no_command_no_registry_falls_through():
    """Known user, no command, flow_registry is None → fallback directly."""
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
    )
    event = _text_event("hello world")
    await router.route(event)
    assert handlers.calls == []
    assert fallback.events == [event]


# --- Router: command dispatch for each command type -----------------------


async def test_router_dispatches_responsibility_snooze():
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
    )
    await router.route(_button_event("action:active-1:snooze"))
    assert handlers.calls == ["snooze:active-1:None"]
    assert fallback.events == []


async def test_router_dispatches_responsibility_snooze_with_preset():
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
    )
    await router.route(_button_event("action:active-1:snooze:1h"))
    assert handlers.calls == ["snooze:active-1:1h"]
    assert fallback.events == []


async def test_router_dispatches_responsibility_dismiss():
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
    )
    await router.route(_button_event("action:active-1:dismiss"))
    assert handlers.calls == ["dismiss:active-1:None"]
    assert fallback.events == []


async def test_router_dispatches_responsibility_dismiss_with_reason():
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
    )
    await router.route(_button_event("dismiss:active-1:not_waiting"))
    assert handlers.calls == ["dismiss:active-1:not_waiting"]
    assert fallback.events == []


async def test_router_dispatches_responsibility_list():
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
    )
    await router.route(_text_event("צפה בשיחות"))
    assert handlers.calls == ["list"]
    assert fallback.events == []


async def test_router_dispatches_digest_open():
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
    )
    await router.route(_text_event("סיכום חדש"))
    assert handlers.calls == ["digest"]
    assert fallback.events == []


async def test_router_dispatches_list_done():
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
    )
    await router.route(_text_event("סיימתי לעבור על רשימת ההמתנה"))
    assert handlers.calls == ["list_done"]
    assert fallback.events == []


async def test_router_dispatches_onboarding_code():
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
    )
    await router.route(_text_event("קוד"))
    assert handlers.calls == ["code:972501234567"]
    assert fallback.events == []


async def test_router_dispatches_onboarding_qr():
    """Known user sending 'qr' re-sends the QR image via handle_onboarding_qr."""
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
    )
    await router.route(_text_event("qr"))
    assert handlers.calls == ["qr:972501234567"]
    assert fallback.events == []


async def test_router_dispatches_onboarding_start_known_user():
    """Known user tapping onboarding:start re-sends QR via handle_onboarding_qr."""
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
    )
    await router.route(_button_event("onboarding:start"))
    assert handlers.calls == ["qr:972501234567"]
    assert fallback.events == []


async def test_router_dispatches_onboarding_info():
    handlers = _FakeHandlers()
    onboarding = _FakeOnboardingEntry()
    fallback = _FakeFallback()
    router = BotCommandRouter(
        user_resolver=_FakeUserResolver(),
        command_handlers=handlers,
        onboarding_entry=onboarding,
        fallback_handler=fallback,
    )
    await router.route(_button_event("onboarding:info"))
    assert handlers.calls == ["info:972501234567"]
    assert fallback.events == []
