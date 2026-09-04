"""Scheduled action repository.

Owns persistence and atomic claiming of :class:`ScheduledAction` rows.
The scheduler uses ``claim_due`` to atomically transition a due
``PENDING`` action to ``IN_PROGRESS``, and ``recover_stale`` on startup
to reset abandoned ``IN_PROGRESS`` actions back to ``PENDING``.

The :class:`ScheduledActionRepository` base class defines the protocol;
:class:`InMemoryScheduledActionRepository` is suitable for tests and
single-process use. A Postgres implementation
(:class:`echo_v2.persistence.postgres_scheduled_actions.PostgresScheduledActionRepository`)
uses ``FOR UPDATE SKIP LOCKED`` for safe concurrent claiming.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)

__all__ = [
    "InMemoryScheduledActionRepository",
    "ScheduledActionRepository",
]


class ScheduledActionRepository:
    """Protocol-style base class for scheduled action repositories.

    Subclasses implement the async methods. Kept as a regular class (not
    ``Protocol``) so it can carry docstrings and be subclassed directly by
    the in-memory impl.
    """

    async def save(self, action: ScheduledAction) -> None:
        """Insert or update a scheduled action (upsert on ``id``)."""
        ...

    async def get(self, action_id: str) -> ScheduledAction | None: ...

    async def list_pending(self, user_id: str) -> list[ScheduledAction]:
        """Return all PENDING actions for a user, ordered by ``execute_at_utc``."""
        ...

    async def claim_due(
        self,
        now: datetime,
        lease_seconds: float = 300.0,
    ) -> ScheduledAction | None:
        """Atomically claim one due PENDING action.

        Transitions the oldest due ``PENDING`` action to ``IN_PROGRESS``,
        sets ``claimed_at = now``, and returns it. Returns ``None`` if no
        action is due. The ``lease_seconds`` parameter defines the stale
        window — if the claim is not resolved within this time,
        ``recover_stale`` may reset it.
        """
        ...

    async def mark_succeeded(
        self,
        action_id: str,
        result: dict[str, Any],
    ) -> None:
        """Mark an action as SUCCEEDED with a result payload."""
        ...

    async def mark_failed(self, action_id: str, error: str) -> None:
        """Mark an action as permanently FAILED."""
        ...

    async def mark_indeterminate(self, action_id: str, error: str) -> None:
        """Mark an action as INDETERMINATE (send outcome unknown)."""
        ...

    async def cancel(self, action_id: str, user_id: str) -> bool:
        """Cancel a PENDING action. Returns ``True`` if cancelled, ``False``
        if not found, not owned by ``user_id``, or not in PENDING status."""
        ...

    async def recover_stale(
        self,
        now: datetime,
        lease_seconds: float = 300.0,
    ) -> int:
        """Reset stale ``IN_PROGRESS`` actions back to ``PENDING``.

        An action is stale if ``claimed_at`` is older than
        ``now - lease_seconds``. Returns the count of recovered actions.
        Called on scheduler startup to recover from crashes.
        """
        ...


class InMemoryScheduledActionRepository(ScheduledActionRepository):
    """Process-local repository backed by a dict.

    Suitable for tests and single-process use. ``claim_due`` uses a simple
    sorted scan — no cross-process coordination.
    """

    def __init__(self) -> None:
        self._actions: dict[str, ScheduledAction] = {}

    async def save(self, action: ScheduledAction) -> None:
        self._actions[action.id] = action

    async def get(self, action_id: str) -> ScheduledAction | None:
        return self._actions.get(action_id)

    async def list_pending(self, user_id: str) -> list[ScheduledAction]:
        pending = [
            a for a in self._actions.values()
            if a.user_id == user_id
            and a.status is ScheduledActionStatus.PENDING
        ]
        return sorted(pending, key=lambda a: a.execute_at_utc)

    async def claim_due(
        self,
        now: datetime,
        lease_seconds: float = 300.0,
    ) -> ScheduledAction | None:
        due = [
            a for a in self._actions.values()
            if a.status is ScheduledActionStatus.PENDING
            and a.execute_at_utc <= now
        ]
        if not due:
            return None
        oldest = min(due, key=lambda a: a.execute_at_utc)
        claimed = replace(
            oldest,
            status=ScheduledActionStatus.IN_PROGRESS,
            claimed_at=now,
        )
        self._actions[oldest.id] = claimed
        return claimed

    async def mark_succeeded(
        self,
        action_id: str,
        result: dict[str, Any],
    ) -> None:
        self._update_terminal(
            action_id,
            status=ScheduledActionStatus.SUCCEEDED,
            result=result,
        )

    async def mark_failed(self, action_id: str, error: str) -> None:
        self._update_terminal(
            action_id,
            status=ScheduledActionStatus.FAILED,
            error=error,
        )

    async def mark_indeterminate(self, action_id: str, error: str) -> None:
        self._update_terminal(
            action_id,
            status=ScheduledActionStatus.INDETERMINATE,
            error=error,
        )

    async def cancel(self, action_id: str, user_id: str) -> bool:
        action = self._actions.get(action_id)
        if action is None:
            return False
        if action.user_id != user_id:
            return False
        if action.status is not ScheduledActionStatus.PENDING:
            return False
        self._actions[action_id] = replace(
            action,
            status=ScheduledActionStatus.CANCELLED,
        )
        return True

    async def recover_stale(
        self,
        now: datetime,
        lease_seconds: float = 300.0,
    ) -> int:
        from datetime import timedelta

        threshold = now - timedelta(seconds=lease_seconds)
        recovered = 0
        for action_id, action in list(self._actions.items()):
            if (
                action.status is ScheduledActionStatus.IN_PROGRESS
                and action.claimed_at is not None
                and action.claimed_at < threshold
            ):
                self._actions[action_id] = replace(
                    action,
                    status=ScheduledActionStatus.PENDING,
                    claimed_at=None,
                )
                recovered += 1
        return recovered

    def _update_terminal(
        self,
        action_id: str,
        *,
        status: ScheduledActionStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        action = self._actions.get(action_id)
        if action is None:
            return
        self._actions[action_id] = replace(
            action,
            status=status,
            result=result,
            error=error,
            executed_at=datetime.now(timezone.utc),
        )
