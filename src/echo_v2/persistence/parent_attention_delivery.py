"""Persistence contracts for parent-attention notification deduplication."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol

from echo_v2.domain.parent_attention import ParentAttentionDeliveryState


class ParentAttentionDeliveryRepository(Protocol):
    async def get(self, *, child_user_id: str, chat_id: str) -> ParentAttentionDeliveryState | None: ...

    async def save(self, state: ParentAttentionDeliveryState) -> None: ...

    async def resolve(self, *, child_user_id: str, chat_id: str, resolved_at: datetime | None = None) -> None: ...


class InMemoryParentAttentionDeliveryRepository:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str], ParentAttentionDeliveryState] = {}

    async def get(self, *, child_user_id: str, chat_id: str) -> ParentAttentionDeliveryState | None:
        return self.states.get((child_user_id, chat_id))

    async def save(self, state: ParentAttentionDeliveryState) -> None:
        self.states[(state.child_user_id, state.chat_id)] = state

    async def resolve(self, *, child_user_id: str, chat_id: str, resolved_at: datetime | None = None) -> None:
        current = await self.get(child_user_id=child_user_id, chat_id=chat_id)
        if current is not None:
            self.states[(child_user_id, chat_id)] = replace(
                current, resolved_at=resolved_at or datetime.now(timezone.utc)
            )


__all__ = [
    "InMemoryParentAttentionDeliveryRepository",
    "ParentAttentionDeliveryRepository",
]
