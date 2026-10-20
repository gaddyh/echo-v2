from __future__ import annotations

import pytest

from echo_v2.runtime.errors import PermanentError
from tests.services.test_scheduling import FakeBot, _make_bot_action, _make_bot_service


class FakeBaileysEventRepository:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.recorded: list[tuple[str, str, str]] = []

    async def record_reconnect_event(
        self, name: str, *, child_id: str, request_id: str, status: str
    ) -> None:
        self.recorded.append((name, child_id, request_id))

    async def mark_notification_sent(self, event_id: str) -> None:
        self.sent.append(event_id)


async def test_scheduled_baileys_action_marks_notification_sent() -> None:
    service, _bot = _make_bot_service()
    repository = FakeBaileysEventRepository()
    service._baileys_event_repo = repository
    action = _make_bot_action(
        payload={
            "chat_id": "972500000001",
            "message": "reauth",
            "idempotency_key": "baileys-reauth:event-1",
            "baileys_event_id": "event-1",
        }
    )
    await service._action_repo.save(action)

    await service.execute(action)

    assert repository.sent == ["event-1"]


async def test_reconnect_template_delivery_records_safe_event() -> None:
    service, bot = _make_bot_service()
    repository = FakeBaileysEventRepository()
    service._baileys_event_repo = repository
    action = _make_bot_action(payload={
        "chat_id": "972500000001",
        "template": {
            "name": "guard_reconnect_v1", "language": "he", "body_params": ["Child"],
            "quick_reply_payload": "guard:reconnect:request-1",
        },
        "baileys_event_id": "event-1",
        "reconnect_request_id": "request-1",
        "reconnect_child_id": "child-id",
    })
    await service._action_repo.save(action)
    await service.execute(action)
    assert repository.recorded == [("reconnect.template_sent", "child-id", "request-1")]
    assert repository.sent == ["event-1"]
    assert bot.template_sends[0][1] == "guard_reconnect_v1"


async def test_reconnect_template_failure_records_only_safe_error_kind() -> None:
    service, _bot = _make_bot_service(bot=FakeBot(fail_with=PermanentError("sensitive-details")))
    repository = FakeBaileysEventRepository()
    service._baileys_event_repo = repository
    action = _make_bot_action(payload={
        "chat_id": "972500000001",
        "template": {
            "name": "guard_reconnect_v1", "language": "he", "body_params": ["Child"],
            "quick_reply_payload": "guard:reconnect:request-1",
        },
        "baileys_event_id": "event-1", "reconnect_request_id": "request-1",
        "reconnect_child_id": "child-id",
    })
    await service._action_repo.save(action)
    with pytest.raises(PermanentError):
        await service.execute(action)
    assert repository.recorded == [("reconnect.template_failed", "child-id", "request-1")]
    assert repository.sent == []
