"""FlowRegistry — checks for active stateful flows.

The :class:`BotCommandRouter` calls the flow registry when no explicit
command matched. If a flow is active, the flow handles the event
according to its own state. If no flow is active, the router falls
through to the fallback handler.

Two flows are checked:

1. **Onboarding flow** — if the user is in the onboarding state
   (``pending``), the :class:`OnboardingService` handles the event
   (e.g. name response before provisioning).

2. **Scheduling flow** — if the user has an active scheduling context
   (not IDLE), the :class:`SchedulingFlowService` handles the event
   (e.g. message text after sending a vCard).

The registry owns the priority between flows. The router does not
know what AWAITING_NAME or AWAITING_MESSAGE means — that's the flow's
responsibility.
"""

from __future__ import annotations

import logging

from echo_v2.domain.conversation import SchedulingFlowState
from echo_v2.ports.bot import BotEvent, BotEventType

__all__ = ["BotFlowRegistry"]

_logger = logging.getLogger("echo_v2.bot.flow_registry")


class BotFlowRegistry:
    """Check for and route to active stateful flows.

    Args:
        onboarding_service: The :class:`OnboardingService`. Its
            ``is_onboarding(phone)`` checks if the user is in the
            onboarding flow, and ``handle_name_response(phone, text)``
            handles text during onboarding.
        scheduling_flow: The :class:`SchedulingFlowService`. Its
            ``_state_repo.get(user_id)`` checks for an active
            scheduling context.
    """

    def __init__(
        self,
        *,
        onboarding_service,
        scheduling_flow,
    ) -> None:
        self._onboarding = onboarding_service
        self._scheduling_flow = scheduling_flow

    async def active_flow_for(
        self, event: BotEvent, user_id: str,
    ) -> bool:
        """If a flow is active, handle the event and return True."""
        # 1. Onboarding flow — if the user is in onboarding, route there.
        is_onboarding = await self._onboarding.is_onboarding(event.user_phone)
        if is_onboarding:
            if event.type is BotEventType.TEXT and event.text:
                handled = await self._onboarding.handle_name_response(
                    event.user_phone, event.text,
                )
                if handled:
                    return True
            # Onboarding user but name response didn't handle it —
            # don't fall through to scheduling. Return True to stop.
            return True

        # 2. Scheduling flow — if the user has an active context.
        ctx = await self._scheduling_flow._state_repo.get(user_id)
        if ctx is not None and ctx.state is not SchedulingFlowState.IDLE:
            await self._scheduling_flow.handle(event)
            return True

        return False
