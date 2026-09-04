"""Scheduler — persistent poller that claims and executes due actions.

The scheduler is **not** an in-memory timer (roadmap §9.5: "An in-memory
timer is not sufficient"). It is a poll loop that:

1. On startup, calls ``recover_stale`` to reset abandoned ``IN_PROGRESS``
   actions back to ``PENDING`` (crash recovery).
2. Repeatedly calls ``claim_due`` to atomically claim one due action,
   then hands it to :class:`SchedulingService` for execution.
3. Handles execution errors gracefully — an indeterminate or permanent
   failure is recorded by the service; the scheduler logs and continues
   to the next action.

The poll interval is configurable. ``run_loop`` runs until cancelled;
``run_once`` processes a single claim-execute cycle (useful for tests and
batch processing).

Concurrency: with the Postgres repository, multiple scheduler workers
can run ``claim_due`` simultaneously — ``FOR UPDATE SKIP LOCKED`` ensures
each worker gets a distinct row. With the in-memory repository, only one
worker should run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from echo_v2.domain.scheduling import ScheduledAction
from echo_v2.persistence.scheduled_actions import ScheduledActionRepository
from echo_v2.runtime.errors import IndeterminateError, PermanentError
from echo_v2.services.scheduling import SchedulingService

__all__ = ["Scheduler"]

_logger = logging.getLogger("echo_v2.services.scheduler")


class Scheduler:
    """Persistent scheduler that claims and executes due actions.

    Args:
        service: The :class:`SchedulingService` that executes actions.
        action_repo: The repository used for ``claim_due`` and
            ``recover_stale``.
        lease_seconds: How long an ``IN_PROGRESS`` claim is valid before
            ``recover_stale`` resets it. Defaults to 5 minutes.
        poll_interval_seconds: How often ``run_loop`` polls for due
            actions. Defaults to 1 second.
    """

    def __init__(
        self,
        service: SchedulingService,
        action_repo: ScheduledActionRepository,
        *,
        lease_seconds: float = 300.0,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._service = service
        self._action_repo = action_repo
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval_seconds

    async def recover(self) -> int:
        """Reset stale ``IN_PROGRESS`` actions to ``PENDING``.

        Call on startup to recover from crashes. Returns the count of
        recovered actions.
        """
        now = datetime.now(timezone.utc)
        recovered = await self._action_repo.recover_stale(now, self._lease_seconds)
        if recovered > 0:
            _logger.info("scheduler recovered %d stale actions", recovered)
        return recovered

    async def run_once(self) -> bool:
        """Claim and execute one due action.

        Returns ``True`` if an action was claimed and processed (regardless
        of success/failure), ``False`` if nothing was due.
        """
        now = datetime.now(timezone.utc)
        action = await self._action_repo.claim_due(now, self._lease_seconds)
        if action is None:
            return False

        await self._execute_action(action)
        return True

    async def run_loop(self) -> None:
        """Poll for due actions until cancelled.

        On each iteration, calls ``run_once``. If nothing was due, sleeps
        for ``poll_interval_seconds``. Handles ``CancelledError`` cleanly.
        """
        while True:
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                _logger.info("scheduler loop cancelled")
                raise
            except Exception:
                # An unexpected error in the loop should not kill the
                # scheduler — log and continue after a pause.
                _logger.exception("scheduler loop error, continuing")
                processed = False

            if not processed:
                try:
                    await asyncio.sleep(self._poll_interval)
                except asyncio.CancelledError:
                    _logger.info("scheduler loop cancelled during sleep")
                    raise

    async def _execute_action(self, action: ScheduledAction) -> None:
        """Execute a claimed action, handling all terminal outcomes."""
        try:
            msg_id = await self._service.execute(action)
            _logger.info(
                "scheduler executed action %s -> provider_message_id=%s",
                action.id,
                msg_id,
            )
        except IndeterminateError as exc:
            # The service already marked the action INDETERMINATE.
            _logger.warning(
                "scheduler action %s indeterminate: %s", action.id, exc
            )
        except PermanentError as exc:
            # The service already marked the action FAILED.
            _logger.warning(
                "scheduler action %s failed: %s", action.id, exc
            )
