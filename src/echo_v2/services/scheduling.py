"""SchedulingService — create, execute, and manage scheduled actions.

The service is the trust-critical core of Step 1 (Scheduled). It:

* Creates ``ScheduledAction`` rows in the repository (PENDING).
* Executes due actions through :func:`echo_v2.runtime.executor.execute`
  with ``EXTERNAL_WRITE`` policy and an idempotency key derived from the
  action id — so a restart or retry **never sends the same WhatsApp
  message twice**.
* Resolves the user's WhatsApp connection at execution time (not at
  creation time), so re-pairing or re-provisioning between scheduling
  and execution is handled naturally.
* Records terminal status (SUCCEEDED / FAILED / INDETERMINATE) and the
  provider message id for delivery tracking.

The idempotency key is ``green:send:{user_id}:{action_id}``. This is the
*logical message identity*: all retries/restarts of the same scheduled
action use the same key. If the send already succeeded, the idempotency
store short-circuits and returns the cached ``provider_message_id``
without calling Green again. If the previous attempt was indeterminate,
the store replays the indeterminate outcome (raises
:class:`IndeterminateError`) — we do not blindly retry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.persistence.scheduled_actions import ScheduledActionRepository
from echo_v2.persistence.whatsapp_connections import (
    WhatsAppConnectionRepository,
)
from echo_v2.ports.bot import BotChannel
from echo_v2.ports.whatsapp import ConnectionRef, WhatsAppMessaging
from echo_v2.runtime.context import RunContext
from echo_v2.runtime.errors import IndeterminateError, PermanentError
from echo_v2.runtime.events import NO_OP_SINK, EventSink
from echo_v2.runtime.executor import execute
from echo_v2.runtime.idempotency import IdempotencyStore
from echo_v2.runtime.policy import EXTERNAL_WRITE

__all__ = ["SchedulingService"]

_logger = logging.getLogger("echo_v2.services.scheduling")


@dataclass(frozen=True)
class _SendInput:
    """Input to the send operation passed through ``runtime.execute``."""

    connection: ConnectionRef
    chat_id: str
    message: str


class SchedulingService:
    """Create and execute scheduled WhatsApp messages."""

    def __init__(
        self,
        action_repo: ScheduledActionRepository,
        connection_repo: WhatsAppConnectionRepository,
        messaging: WhatsAppMessaging,
        idempotency_store: IdempotencyStore[str],
        event_sink: EventSink | None = None,
        bot_channel: BotChannel | None = None,
    ) -> None:
        self._action_repo = action_repo
        self._connection_repo = connection_repo
        self._messaging = messaging
        self._idempotency_store = idempotency_store
        self._event_sink = event_sink or NO_OP_SINK
        self._bot_channel = bot_channel

    async def create(
        self,
        user_id: str,
        type: ScheduledActionType,
        execute_at_utc: datetime,
        timezone_name: str,
        payload: dict[str, Any],
    ) -> ScheduledAction:
        """Create and persist a new PENDING scheduled action."""
        action = ScheduledAction(
            id=str(uuid4()),
            user_id=user_id,
            type=type,
            execute_at_utc=execute_at_utc,
            timezone=timezone_name,
            status=ScheduledActionStatus.PENDING,
            payload=payload,
        )
        await self._action_repo.save(action)
        return action

    async def list_pending(self, user_id: str) -> list[ScheduledAction]:
        """List all PENDING actions for a user."""
        return await self._action_repo.list_pending(user_id)

    async def cancel(self, action_id: str, user_id: str) -> bool:
        """Cancel a PENDING action."""
        return await self._action_repo.cancel(action_id, user_id)

    async def execute(self, action: ScheduledAction) -> str:
        """Execute a scheduled action.

        For ``SEND_WHATSAPP_MESSAGE``: resolves the user's Green connection,
        runs the send via ``runtime.execute(EXTERNAL_WRITE)`` with idempotency.

        For ``SEND_BOT_MESSAGE``: sends a reminder via the bot channel
        (360dialog). No idempotency needed — a duplicate bot message is
        annoying but not irreversible.

        Raises ``ValueError`` for unsupported action types.
        """
        if action.type is ScheduledActionType.SEND_WHATSAPP_MESSAGE:
            return await self._execute_green_send(action)
        if action.type is ScheduledActionType.SEND_BOT_MESSAGE:
            return await self._execute_bot_send(action)
        raise ValueError(f"unsupported action type: {action.type}")

    async def _execute_green_send(self, action: ScheduledAction) -> str:
        """Execute a Green API WhatsApp message send with idempotency."""
        # Resolve the user's WhatsApp connection at execution time.
        conn = await self._connection_repo.get_by_user(action.user_id)
        if conn is None:
            error = f"no WhatsApp connection for user {action.user_id}"
            await self._action_repo.mark_failed(action.id, error)
            raise PermanentError(error)

        chat_id = action.payload.get("chat_id", "")
        message = action.payload.get("message", "")
        if not chat_id or not message:
            error = f"action {action.id} payload missing chat_id or message"
            await self._action_repo.mark_failed(action.id, error)
            raise PermanentError(error)

        send_input = _SendInput(
            connection=conn.ref,
            chat_id=chat_id,
            message=message,
        )
        idempotency_key = f"green:send:{action.user_id}:{action.id}"
        context = RunContext(operation_name="scheduled_send")

        try:
            result = await execute(
                operation=self._send_operation,
                input_=send_input,
                context=context,
                policy=EXTERNAL_WRITE,
                event_sink=self._event_sink,
                idempotency_key=idempotency_key,
                idempotency_store=self._idempotency_store,
            )
        except IndeterminateError as exc:
            await self._action_repo.mark_indeterminate(action.id, str(exc))
            raise
        except PermanentError as exc:
            await self._action_repo.mark_failed(action.id, str(exc))
            raise

        provider_message_id = result.value
        await self._action_repo.mark_succeeded(
            action.id,
            {"provider_message_id": provider_message_id},
        )
        return provider_message_id

    async def _execute_bot_send(self, action: ScheduledAction) -> str:
        """Execute a bot-channel reminder send (no idempotency needed)."""
        if self._bot_channel is None:
            error = "no bot channel configured for SEND_BOT_MESSAGE"
            await self._action_repo.mark_failed(action.id, error)
            raise PermanentError(error)

        chat_id = action.payload.get("chat_id", "")
        message = action.payload.get("message", "")
        if not chat_id or not message:
            error = f"action {action.id} payload missing chat_id or message"
            await self._action_repo.mark_failed(action.id, error)
            raise PermanentError(error)

        try:
            await self._bot_channel.send_text(chat_id, message)
        except Exception as exc:
            await self._action_repo.mark_failed(action.id, str(exc))
            raise

        await self._action_repo.mark_succeeded(action.id, {"sent": True})
        return "bot_sent"

    async def _send_operation(self, inp: _SendInput) -> str:
        """The actual send, called by ``runtime.execute``."""
        return await self._messaging.send_message(
            inp.connection,
            inp.chat_id,
            inp.message,
        )
