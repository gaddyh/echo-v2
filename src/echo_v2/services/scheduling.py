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
from uuid import NAMESPACE_URL, uuid4, uuid5

from langsmith import traceable

from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.observability.sanitizers import (
    safe_bot_send_inputs,
    safe_bot_send_output,
    safe_scheduling_execute_inputs,
    safe_scheduling_execute_output,
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


@dataclass(frozen=True)
class _BotSendInput:
    """Input to a bot-channel send passed through ``runtime.execute``."""

    chat_id: str
    message: str
    buttons: list[dict] | None = None


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
        send_validator=None,
    ) -> None:
        self._action_repo = action_repo
        self._connection_repo = connection_repo
        self._messaging = messaging
        self._idempotency_store = idempotency_store
        self._event_sink = event_sink or NO_OP_SINK
        self._bot_channel = bot_channel
        self._send_validator = send_validator

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

    async def create_once(
        self,
        *,
        request_id: str,
        user_id: str,
        type: ScheduledActionType,
        execute_at_utc: datetime,
        timezone_name: str,
        payload: dict[str, Any],
    ) -> tuple[ScheduledAction, bool]:
        """Idempotently create a PENDING scheduled action.

        The action id is derived deterministically from ``user_id`` and
        ``request_id`` via ``uuid5(NAMESPACE_URL, ...)``. The repository's
        :meth:`create_once` performs an ``INSERT ... ON CONFLICT DO NOTHING``.
        A retry with the same ``request_id`` returns the original action
        with ``created=False`` and does NOT overwrite its status or payload.

        Returns ``(action, created)``.
        """
        action_id = str(uuid5(NAMESPACE_URL, f"echo:send:{user_id}:{request_id}"))
        action = ScheduledAction(
            id=action_id,
            user_id=user_id,
            type=type,
            execute_at_utc=execute_at_utc,
            timezone=timezone_name,
            status=ScheduledActionStatus.PENDING,
            payload=payload,
        )
        return await self._action_repo.create_once(action)

    async def list_pending(self, user_id: str) -> list[ScheduledAction]:
        """List all PENDING actions for a user."""
        return await self._action_repo.list_pending(user_id)

    async def cancel(self, action_id: str, user_id: str) -> bool:
        """Cancel a PENDING action."""
        return await self._action_repo.cancel(action_id, user_id)

    @traceable(
        name="wfm.scheduling.execute",
        process_inputs=safe_scheduling_execute_inputs,
        process_outputs=safe_scheduling_execute_output,
    )
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

    @traceable(
        name="wfm.scheduling.bot_send",
        process_inputs=safe_bot_send_inputs,
        process_outputs=safe_bot_send_output,
    )
    async def _execute_bot_send(self, action: ScheduledAction) -> str:
        """Execute a bot-channel reminder send through the runtime executor.

        Routes through :func:`echo_v2.runtime.executor.execute` with
        ``EXTERNAL_WRITE`` policy and idempotency — same guardrails as
        Green sends. A duplicate bot message is annoying, but the
        executor also provides retry, error classification, and event
        emission that we want for all external writes.

        Payload:
        * ``chat_id`` (required) — recipient phone number.
        * ``message`` (required for text) — body text.
        * ``buttons`` (optional) — list of ``{id, title}`` dicts. When
          present, sends an interactive button message instead of text.
        * ``kind`` (optional) — when ``"waiting_for_me_reminder"``, the
          ``send_validator`` is called to check whether the reminder is
          still relevant. If it returns ``False``, the send is skipped.
        """
        # Self-validation for waiting-for-me reminders (before executor).
        if (
            self._send_validator is not None
            and action.payload.get("kind") == "waiting_for_me_reminder"
        ):
            should_send = await self._send_validator(action.payload)
            if not should_send:
                _logger.info(
                    "scheduling: skipping stale reminder action %s "
                    "(active item no longer matches)",
                    action.id,
                )
                await self._action_repo.mark_succeeded(
                    action.id, {"skipped": True}
                )
                return "bot_skipped"

        if self._bot_channel is None:
            error = "no bot channel configured for SEND_BOT_MESSAGE"
            await self._action_repo.mark_failed(action.id, error)
            raise PermanentError(error)

        chat_id = action.payload.get("chat_id", "")
        message = action.payload.get("message", "")
        buttons = action.payload.get("buttons")

        if not chat_id:
            error = f"action {action.id} payload missing chat_id"
            await self._action_repo.mark_failed(action.id, error)
            raise PermanentError(error)

        if buttons and not message:
            error = f"action {action.id} payload missing message for buttons"
            await self._action_repo.mark_failed(action.id, error)
            raise PermanentError(error)

        if not buttons and not message:
            error = f"action {action.id} payload missing message"
            await self._action_repo.mark_failed(action.id, error)
            raise PermanentError(error)

        send_input = _BotSendInput(
            chat_id=chat_id,
            message=message,
            buttons=buttons,
        )
        idempotency_key = f"bot:send:{action.user_id}:{action.id}"
        context = RunContext(operation_name="scheduled_bot_send")

        try:
            result = await execute(
                operation=self._bot_send_operation,
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
            {"sent": True, "provider_message_id": provider_message_id},
        )
        return provider_message_id or "bot_sent"

    async def _send_operation(self, inp: _SendInput) -> str:
        """The actual send, called by ``runtime.execute``."""
        return await self._messaging.send_message(
            inp.connection,
            inp.chat_id,
            inp.message,
        )

    async def _bot_send_operation(self, inp: _BotSendInput) -> str:
        """The actual bot send, called by ``runtime.execute``."""
        if inp.buttons:
            msg_id = await self._bot_channel.send_buttons(
                inp.chat_id,
                body_text=inp.message,
                buttons=inp.buttons,
            )
            return msg_id or "bot_sent"
        await self._bot_channel.send_text(inp.chat_id, inp.message)
        return "bot_sent"
