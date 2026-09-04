"""Tests for SchedulingService: idempotent send, status transitions, no double-send."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.observability import InMemoryEventSink
from echo_v2.persistence.scheduled_actions import InMemoryScheduledActionRepository
from echo_v2.persistence.whatsapp_connections import (
    InMemoryWhatsAppConnectionRepository,
    StoredConnection,
)
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ConnectionStatus,
    ProviderCredentials,
    WhatsAppMessaging,
)
from echo_v2.runtime.errors import IndeterminateError, PermanentError
from echo_v2.runtime.idempotency import InMemoryIdempotencyStore
from echo_v2.services.scheduling import SchedulingService


# --- fakes -----------------------------------------------------------------


class FakeMessaging:
    """Records send calls; can be configured to fail."""

    def __init__(
        self,
        *,
        msg_id: str = "MSG_1",
        fail_with: Exception | None = None,
    ) -> None:
        self.msg_id = msg_id
        self.fail_with = fail_with
        self.send_count = 0
        self.calls: list[tuple[ConnectionRef, str, str]] = []

    async def send_message(
        self,
        connection: ConnectionRef,
        chat_id: str,
        message: str,
    ) -> str:
        self.send_count += 1
        self.calls.append((connection, chat_id, message))
        if self.fail_with is not None:
            raise self.fail_with
        return self.msg_id


def _make_connection_repo(user_id: str = "user-1") -> InMemoryWhatsAppConnectionRepository:
    repo = InMemoryWhatsAppConnectionRepository()
    repo._by_ref[("green", "123")] = StoredConnection(
        user_id=user_id,
        ref=ConnectionRef("green", "123"),
        credentials=ProviderCredentials(b"api-tok"),
        webhook_token_hash=b"\x00" * 32,
        status=ConnectionStatus.CONNECTED,
    )
    repo._by_user[user_id] = ("green", "123")
    return repo


def _make_service(
    *,
    messaging: FakeMessaging | None = None,
    idempotency: InMemoryIdempotencyStore | None = None,
    connection_repo: InMemoryWhatsAppConnectionRepository | None = None,
    event_sink: InMemoryEventSink | None = None,
    user_id: str = "user-1",
) -> tuple[SchedulingService, FakeMessaging, InMemoryIdempotencyStore]:
    messaging = messaging or FakeMessaging()
    idempotency = idempotency or InMemoryIdempotencyStore()
    connection_repo = connection_repo or _make_connection_repo(user_id)
    sink = event_sink or InMemoryEventSink()
    service = SchedulingService(
        action_repo=InMemoryScheduledActionRepository(),
        connection_repo=connection_repo,
        messaging=messaging,
        idempotency_store=idempotency,
        event_sink=sink,
    )
    return service, messaging, idempotency


def _make_pending_action(
    *,
    id: str = "act-1",
    user_id: str = "user-1",
    payload: dict[str, Any] | None = None,
) -> ScheduledAction:
    return ScheduledAction(
        id=id,
        user_id=user_id,
        type=ScheduledActionType.SEND_WHATSAPP_MESSAGE,
        execute_at_utc=datetime(2026, 9, 5, 8, tzinfo=timezone.utc),
        timezone="Asia/Jerusalem",
        status=ScheduledActionStatus.PENDING,
        payload=payload or {"chat_id": "972@c.us", "message": "hello"},
    )


# --- protocol check --------------------------------------------------------


def test_fake_messaging_satisfies_port():
    assert isinstance(FakeMessaging(), WhatsAppMessaging)


# --- create / list / cancel ------------------------------------------------


async def test_create_persists_pending_action():
    service, _, _ = _make_service()
    action = await service.create(
        user_id="user-1",
        type=ScheduledActionType.SEND_WHATSAPP_MESSAGE,
        execute_at_utc=datetime(2026, 9, 5, 8, tzinfo=timezone.utc),
        timezone_name="Asia/Jerusalem",
        payload={"chat_id": "972@c.us", "message": "hi"},
    )
    assert action.status is ScheduledActionStatus.PENDING
    assert action.id  # uuid assigned
    pending = await service.list_pending("user-1")
    assert len(pending) == 1
    assert pending[0].id == action.id


async def test_cancel_pending_action():
    service, _, _ = _make_service()
    action = await service.create(
        user_id="user-1",
        type=ScheduledActionType.SEND_WHATSAPP_MESSAGE,
        execute_at_utc=datetime(2026, 9, 5, 8, tzinfo=timezone.utc),
        timezone_name="Asia/Jerusalem",
        payload={"chat_id": "972@c.us", "message": "hi"},
    )
    assert await service.cancel(action.id, "user-1") is True
    assert await service.cancel(action.id, "user-1") is False  # already cancelled


# --- execute: success ------------------------------------------------------


async def test_execute_sends_and_marks_succeeded():
    service, messaging, _ = _make_service()
    action = _make_pending_action()
    await service._action_repo.save(action)

    msg_id = await service.execute(action)
    assert msg_id == "MSG_1"
    assert messaging.send_count == 1

    fetched = await service._action_repo.get("act-1")
    assert fetched.status is ScheduledActionStatus.SUCCEEDED
    assert fetched.result == {"provider_message_id": "MSG_1"}
    assert fetched.executed_at is not None


async def test_execute_resolves_connection_at_execution_time():
    service, messaging, _ = _make_service()
    action = _make_pending_action()
    await service._action_repo.save(action)

    await service.execute(action)
    conn_ref, chat_id, message = messaging.calls[0]
    assert conn_ref == ConnectionRef("green", "123")
    assert chat_id == "972@c.us"
    assert message == "hello"


# --- execute: idempotency prevents double-send ----------------------------


async def test_execute_replay_does_not_resend():
    """Calling execute twice on the same action must not send twice."""
    service, messaging, _ = _make_service()
    action = _make_pending_action()
    await service._action_repo.save(action)

    first = await service.execute(action)
    # Second call: the idempotency store has a cached SUCCESS outcome.
    second = await service.execute(action)
    assert first == second == "MSG_1"
    assert messaging.send_count == 1


async def test_execute_after_restart_replays_cached_success():
    """Simulate a restart: new service instance, same idempotency store."""
    idempotency = InMemoryIdempotencyStore()
    service1, messaging, _ = _make_service(idempotency=idempotency)
    action = _make_pending_action()
    await service1._action_repo.save(action)
    await service1.execute(action)

    # New service with the SAME idempotency store (simulating persistent store).
    service2, _, _ = _make_service(
        messaging=messaging,
        idempotency=idempotency,
    )
    # Re-save the action (new service has a fresh in-memory action repo).
    await service2._action_repo.save(action)
    msg_id = await service2.execute(action)
    assert msg_id == "MSG_1"
    assert messaging.send_count == 1  # still only one send


# --- execute: indeterminate ------------------------------------------------


async def test_execute_indeterminate_marks_action_and_reraises():
    from echo_v2.integrations.green.client import GreenApiIndeterminateError

    messaging = FakeMessaging(fail_with=GreenApiIndeterminateError("timeout"))
    service, _, _ = _make_service(messaging=messaging)
    action = _make_pending_action()
    await service._action_repo.save(action)

    with pytest.raises(IndeterminateError):
        await service.execute(action)

    fetched = await service._action_repo.get("act-1")
    assert fetched.status is ScheduledActionStatus.INDETERMINATE
    assert fetched.error is not None


async def test_execute_indeterminate_replay_does_not_resend():
    """A second execute after indeterminate replays the indeterminate outcome."""
    from echo_v2.integrations.green.client import GreenApiIndeterminateError

    messaging = FakeMessaging(fail_with=GreenApiIndeterminateError("timeout"))
    service, _, _ = _make_service(messaging=messaging)
    action = _make_pending_action()
    await service._action_repo.save(action)

    with pytest.raises(IndeterminateError):
        await service.execute(action)
    assert messaging.send_count == 1

    # Second call: idempotency store replays INDETERMINATE, does not call Green.
    with pytest.raises(IndeterminateError):
        await service.execute(action)
    assert messaging.send_count == 1


# --- execute: permanent failure -------------------------------------------


async def test_execute_permanent_failure_marks_failed_and_reraises():
    from echo_v2.integrations.green.client import GreenApiError

    messaging = FakeMessaging(fail_with=GreenApiError("bad request"))
    service, _, _ = _make_service(messaging=messaging)
    action = _make_pending_action()
    await service._action_repo.save(action)

    with pytest.raises(PermanentError):
        await service.execute(action)

    fetched = await service._action_repo.get("act-1")
    assert fetched.status is ScheduledActionStatus.FAILED
    assert fetched.error is not None


# --- execute: missing connection ------------------------------------------


async def test_execute_no_connection_marks_failed():
    service, _, _ = _make_service(connection_repo=InMemoryWhatsAppConnectionRepository())
    action = _make_pending_action()
    await service._action_repo.save(action)

    with pytest.raises(PermanentError):
        await service.execute(action)

    fetched = await service._action_repo.get("act-1")
    assert fetched.status is ScheduledActionStatus.FAILED


# --- execute: bad payload --------------------------------------------------


async def test_execute_missing_chat_id_marks_failed():
    service, _, _ = _make_service()
    action = _make_pending_action(payload={"message": "hi"})  # no chat_id
    await service._action_repo.save(action)

    with pytest.raises(PermanentError):
        await service.execute(action)

    fetched = await service._action_repo.get("act-1")
    assert fetched.status is ScheduledActionStatus.FAILED


# --- execute: unsupported type ---------------------------------------------


async def test_execute_unsupported_type_raises_value_error():
    service, _, _ = _make_service()
    action = ScheduledAction(
        id="act-1",
        user_id="user-1",
        type=ScheduledActionType.SEND_REMINDER,
        execute_at_utc=datetime(2026, 9, 5, 8, tzinfo=timezone.utc),
        timezone="Asia/Jerusalem",
        status=ScheduledActionStatus.PENDING,
        payload={"message": "reminder"},
    )
    await service._action_repo.save(action)

    with pytest.raises(ValueError):
        await service.execute(action)


# --- idempotency key shape -------------------------------------------------


async def test_idempotency_key_includes_user_and_action_id():
    """The idempotency key must be deterministic per logical action."""
    service, _, idempotency = _make_service()
    action = _make_pending_action(id="act-xyz")
    await service._action_repo.save(action)
    await service.execute(action)

    cached = await idempotency.get(f"green:send:user-1:act-xyz")
    assert cached is not None
    assert cached.value == "MSG_1"
