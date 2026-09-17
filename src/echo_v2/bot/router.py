"""BotCommandRouter — explicit command dispatch for the 360dialog webhook.

Replaces the chain of pre-handlers (feedback → onboarding → scheduling)
with a single explicit router:

    1. Resolve actor (user lookup by phone)
    2. Unknown → onboarding entry (consent / intro)
    3. Known → parse explicit command from event
    4. If command → dispatch to command handler
    5. If no command → active stateful flow (onboarding / scheduling)
    6. If no flow → fallback (scheduling flow handles generic text)

Priority: explicit command > active conversational flow > generic fallback.

The router owns routing only. Business logic stays in the existing
services (FeedbackHandler, OnboardingService, SchedulingFlowService).
Stateful flows interpret events according to their own state — the
router does not know what AWAITING_NAME or AWAITING_MESSAGE means.
"""

from __future__ import annotations

import logging
from typing import Protocol

from echo_v2.bot.commands import (
    BotCommand,
    BotCommandParser,
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
from echo_v2.ports.bot import BotEvent

__all__ = ["BotCommandRouter", "CommandHandlers", "FlowRegistry", "OnboardingEntry"]

_logger = logging.getLogger("echo_v2.bot.router")


class CommandHandlers(Protocol):
    """Service methods invoked by the router for each command type.

    Implemented by :class:`FeedbackHandler` (responsibility commands,
    digest, list). Onboarding commands are dispatched to
    :class:`OnboardingService` via :attr:`BotCommandRouter._onboarding`.
    """

    async def handle_responsibility_done(
        self, event: BotEvent, active_id: str,
    ) -> None: ...

    async def handle_responsibility_snooze(
        self, event: BotEvent, active_id: str, preset: str | None,
    ) -> None: ...

    async def handle_responsibility_dismiss(
        self, event: BotEvent, active_id: str, reason: str | None,
    ) -> None: ...

    async def handle_responsibility_list(self, event: BotEvent) -> None: ...

    async def handle_digest_open(self, event: BotEvent) -> None: ...

    async def handle_list_done(self, event: BotEvent) -> None: ...


class OnboardingEntry(Protocol):
    """Onboarding service methods invoked by the router.

    Implemented by :class:`OnboardingService`. The router dispatches
    onboarding commands (``OnboardingCode``, ``OnboardingQr``,
    ``OnboardingStart``, ``OnboardingInfo``) here, and routes unknown
    users to ``handle_unknown_event``.
    """

    async def handle_unknown_event(self, event: BotEvent) -> None: ...

    async def handle_onboarding_code(self, phone: str) -> bool: ...

    async def handle_onboarding_qr(self, phone: str) -> bool: ...

    async def handle_onboarding_start(self, phone: str) -> None: ...

    async def handle_onboarding_info(self, phone: str) -> None: ...


class FlowRegistry(Protocol):
    """Check for and route to active stateful flows.

    The router calls ``active_flow_for`` when no explicit command
    matched. If a flow is active, the flow handles the event according
    to its own state. If no flow is active, the router falls through
    to the fallback handler.
    """

    async def active_flow_for(
        self, event: BotEvent, user_id: str,
    ) -> bool:
        """If a flow is active, handle the event and return True.

        Return False if no active flow — the router falls through to
        the fallback handler.
        """
        ...


class BotCommandRouter:
    """Route bot events to handlers via typed commands.

    The router is the single entry point for the 360dialog webhook's
    ``_dispatch``. It replaces the chain of pre-handlers with an
    explicit: resolve actor → parse command → dispatch / flow / fallback.

    Args:
        user_resolver: Maps phone → (user_id, ...). Returns ``None``
            for unknown users.
        command_handlers: Service with per-command methods (see
            :class:`CommandHandlers` protocol).
        onboarding_entry: Service with ``handle_unknown_event(event)``
            for unknown users and ``is_onboarding(phone)`` /
            ``handle_name_response(phone, text)`` for the onboarding
            flow.
        flow_registry: Optional :class:`FlowRegistry` for stateful
            flows. If ``None``, the router falls through to the
            fallback directly.
        fallback_handler: Callable that handles events when no command
            and no active flow matched. Typically the scheduling flow.
        command_parser: Optional parser override (defaults to
            :class:`BotCommandParser`).
    """

    def __init__(
        self,
        *,
        user_resolver,  # UserResolver: phone → (user_id, ...) | None
        command_handlers: CommandHandlers,
        onboarding_entry: OnboardingEntry,
        fallback_handler,  # callable(event) -> None
        flow_registry: FlowRegistry | None = None,
        command_parser: BotCommandParser | None = None,
    ) -> None:
        self._user_resolver = user_resolver
        self._handlers = command_handlers
        self._onboarding = onboarding_entry
        self._fallback = fallback_handler
        self._flow_registry = flow_registry
        self._parser = command_parser or BotCommandParser()

    async def route(self, event: BotEvent) -> None:
        """Route an incoming bot event to the appropriate handler."""
        # 1. Resolve actor.
        user_info = await self._user_resolver.resolve(event.user_phone)

        # 2. Unknown → onboarding entry (consent / intro).
        if user_info is None:
            await self._onboarding.handle_unknown_event(event)
            return

        # 3. Parse explicit command.
        command = self._parser.parse(event)

        # 4. If command → dispatch.
        if command is not None:
            await self._dispatch_command(command, event)
            return

        # 5. If no command → active stateful flow.
        if self._flow_registry is not None:
            user_id = user_info[0]
            handled = await self._flow_registry.active_flow_for(
                event, user_id,
            )
            if handled:
                return

        # 6. Fallback (scheduling flow handles generic text).
        await self._fallback(event)

    async def _dispatch_command(
        self, command: BotCommand, event: BotEvent,
    ) -> None:
        """Dispatch a typed command to the appropriate handler."""
        match command:
            case ResponsibilityDone(responsibility_id=active_id):
                await self._handlers.handle_responsibility_done(
                    event, active_id,
                )
            case ResponsibilitySnooze(
                responsibility_id=active_id, preset=preset,
            ):
                await self._handlers.handle_responsibility_snooze(
                    event, active_id, preset,
                )
            case ResponsibilityDismiss(
                responsibility_id=active_id, reason=reason,
            ):
                await self._handlers.handle_responsibility_dismiss(
                    event, active_id, reason,
                )
            case ResponsibilityList():
                await self._handlers.handle_responsibility_list(event)
            case DigestOpen():
                await self._handlers.handle_digest_open(event)
            case ListDone():
                await self._handlers.handle_list_done(event)
            case OnboardingCode():
                await self._onboarding.handle_onboarding_code(event.user_phone)
            case OnboardingQr():
                await self._onboarding.handle_onboarding_qr(event.user_phone)
            case OnboardingStart():
                # Known user tapping onboarding:start — idempotent re-entry
                # (re-send QR if pending, re-ask name if needed, skip if active).
                await self._onboarding.handle_onboarding_start(event.user_phone)
            case OnboardingInfo():
                await self._onboarding.handle_onboarding_info(event.user_phone)
            case Cancel():
                # Cancel is a global command — pass to the fallback
                # (scheduling flow handles cancel internally).
                await self._fallback(event)
