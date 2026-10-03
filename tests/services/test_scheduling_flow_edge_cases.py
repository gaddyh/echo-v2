from __future__ import annotations

from tests.services.test_scheduling_flow import (
    _contact_event,
    _make_flow_service,
    _text_event,
)


async def test_awaiting_time_success_path_is_reached() -> None:
    flow, bot, action_repo = _make_flow_service()
    await flow.handle(_contact_event(name="Dana"))
    await flow.handle(_text_event("call me"))
    await flow.handle(_text_event("בעוד שעה", event_id="edge-time"))

    assert len(await action_repo.list_pending("user-1")) == 1
    assert len(bot.sent) == 3
