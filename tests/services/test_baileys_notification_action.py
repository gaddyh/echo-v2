from __future__ import annotations

from tests.services.test_scheduling import _make_bot_action, _make_bot_service


class FakeBaileysEventRepository:
    def __init__(self) -> None:
        self.sent: list[str] = []

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
