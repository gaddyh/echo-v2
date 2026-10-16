"""Identity-integrity tests for Baseline B conversation construction."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from echo_v2.domain.chat import Message
from echo_v2.persistence.chat_repositories import InMemoryMessageRepository
from echo_v2.ports.whatsapp import MessageDirection
from echo_v2.services.guard_conversation import SimpleGuardConversationBuilder

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 16, 10, tzinfo=timezone.utc)


def make_message(
    message_id: str,
    *,
    direction: MessageDirection,
    sender_id: str | None,
    connection_id: str = "connection",
    chat_id: str = "chat@c.us",
) -> Message:
    return Message(
        id=message_id,
        user_id="child",
        connection_id=connection_id,
        chat_id=chat_id,
        provider_message_id=f"provider-{message_id}",
        direction=direction,
        sender_id=sender_id,
        timestamp=NOW,
        text=message_id,
    )


async def build(
    messages: list[Message],
    *,
    connection_id: str = "connection",
    chat_id: str = "chat@c.us",
    max_messages: int = 100,
):
    repository = InMemoryMessageRepository()
    for item in messages:
        await repository.save(item)
    return await SimpleGuardConversationBuilder(repository, max_messages=max_messages).build(
        child_user_id="child",
        child_name="Shay",
        connection_id=connection_id,
        chat_id=chat_id,
        timezone_name="Asia/Jerusalem",
        as_of=NOW,
    )


async def test_private_inbound_and_outbound_roles_are_direction_based() -> None:
    conversation = await build(
        [
            make_message("inbound", direction=MessageDirection.INBOUND, sender_id="parent"),
            make_message("outbound", direction=MessageDirection.OUTBOUND, sender_id=None),
        ]
    )

    roles = {message.id: message.sender for message in conversation.messages}
    assert roles == {"inbound": "other", "outbound": "child"}
    assert conversation.identity_verified


async def test_group_participants_have_stable_anonymous_roles() -> None:
    conversation = await build(
        [
            make_message("z", direction=MessageDirection.INBOUND, sender_id="z", chat_id="group@g.us"),
            make_message("a", direction=MessageDirection.INBOUND, sender_id="a", chat_id="group@g.us"),
        ],
        chat_id="group@g.us",
    )

    roles = {message.id: message.sender for message in conversation.messages}
    assert roles == {"z": "other_2", "a": "other"}
    assert conversation.identity_verified


async def test_echo_like_inbound_message_is_not_relabelled_as_child() -> None:
    conversation = await build(
        [
            make_message("outbound", direction=MessageDirection.OUTBOUND, sender_id=None),
            make_message("echo", direction=MessageDirection.INBOUND, sender_id="same-text"),
        ]
    )

    roles = {message.id: message.sender for message in conversation.messages}
    assert roles == {"outbound": "child", "echo": "other"}


async def test_mixed_connections_fail_closed() -> None:
    conversation = await build(
        [
            make_message("one", direction=MessageDirection.INBOUND, sender_id="a"),
            make_message(
                "two",
                direction=MessageDirection.INBOUND,
                sender_id="b",
                connection_id="different-connection",
            ),
        ]
    )

    assert not conversation.identity_verified
    assert "connection" in (conversation.identity_reason or "")


async def test_connection_mismatch_hidden_by_cap_still_fails_closed() -> None:
    conversation = await build(
        [
            make_message(
                "old",
                direction=MessageDirection.INBOUND,
                sender_id="a",
                connection_id="different-connection",
            ),
            make_message("new", direction=MessageDirection.INBOUND, sender_id="b"),
        ],
        max_messages=1,
    )

    assert not conversation.identity_verified
    assert "connection" in (conversation.identity_reason or "")
