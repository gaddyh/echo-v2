"""In-memory conversation state repository for the scheduling flow.

Ephemeral per-user state — no persistence needed across restarts (the
scheduling flow has no external side effects until the action is created).
A Postgres implementation can be added later if we want flow state to
survive restarts, but for the MVP in-memory is sufficient.
"""

from __future__ import annotations

from echo_v2.domain.conversation import SchedulingFlowContext

__all__ = ["ConversationStateRepository", "InMemoryConversationStateRepository"]


class ConversationStateRepository:
    """Protocol-style base class for conversation state repositories."""

    async def get(self, user_id: str) -> SchedulingFlowContext: ...

    async def save(self, context: SchedulingFlowContext) -> None: ...

    async def delete(self, user_id: str) -> None: ...


class InMemoryConversationStateRepository(ConversationStateRepository):
    """Process-local conversation state backed by a dict."""

    def __init__(self) -> None:
        self._states: dict[str, SchedulingFlowContext] = {}

    async def get(self, user_id: str) -> SchedulingFlowContext:
        return self._states.get(
            user_id,
            SchedulingFlowContext(user_id=user_id),
        )

    async def save(self, context: SchedulingFlowContext) -> None:
        self._states[context.user_id] = context

    async def delete(self, user_id: str) -> None:
        self._states.pop(user_id, None)
