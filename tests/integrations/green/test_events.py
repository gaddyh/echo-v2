"""Tests for GreenEventAdapter: Green webhook JSON -> provider events."""

from __future__ import annotations

from datetime import datetime, timezone

from echo_v2.integrations.green.events import GreenEventAdapter
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ConnectionStatus,
    MessageDirection,
    MessageKind,
    MessageSource,
    ProviderConnectionStateChanged,
    ProviderMessageEvent,
    ProviderMessageStatusEvent,
    WhatsAppEventAdapter,
)


def test_adapter_satisfies_event_adapter_protocol():
    assert isinstance(GreenEventAdapter(), WhatsAppEventAdapter)


def _base(type_webhook: str, **extra) -> dict:
    payload = {
        "typeWebhook": type_webhook,
        "instanceData": {"idInstance": 123},
        "timestamp": 1700000000,
    }
    payload.update(extra)
    return payload


def test_parse_incoming_message():
    payload = _base(
        "incomingMessageReceived",
        chatId="9725@c.us",
        idMessage="m1",
        messageData={"typeMessage": "textMessage", "textMessage": "hi"},
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.connection == ConnectionRef("green", "123")
    assert event.direction is MessageDirection.INBOUND
    assert event.source is None
    assert event.chat_id == "9725@c.us"
    assert event.provider_message_id == "m1"
    assert event.kind is MessageKind.TEXT
    assert event.text == "hi"
    assert event.timestamp == datetime.fromtimestamp(1700000000, timezone.utc)


def test_parse_outgoing_user_message():
    payload = _base(
        "outgoingMessageReceived",
        chatId="9725@c.us",
        idMessage="m2",
        messageData={
            "typeMessage": "extendedTextMessage",
            "extendedTextMessage": {"text": "reply"},
        },
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.direction is MessageDirection.OUTBOUND
    assert event.source is MessageSource.USER
    assert event.text == "reply"
    assert event.kind is MessageKind.TEXT


def test_parse_outgoing_api_message():
    payload = _base(
        "outgoingAPIMessageReceived",
        chatId="9725@c.us",
        idMessage="m3",
        messageData={"typeMessage": "textMessage", "textMessage": "echo sent this"},
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.direction is MessageDirection.OUTBOUND
    assert event.source is MessageSource.API


def test_parse_outgoing_status():
    payload = _base(
        "outgoingMessageStatus",
        idMessage="m3",
        messageData={"statusWebhook": "delivered"},
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageStatusEvent)
    assert event.provider_message_id == "m3"
    assert event.status == "delivered"
    assert event.connection == ConnectionRef("green", "123")


def test_parse_state_instance_changed_authorized():
    payload = _base(
        "stateInstanceChanged",
        instanceData={"idInstance": 123, "stateInstance": "authorized"},
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderConnectionStateChanged)
    assert event.status is ConnectionStatus.CONNECTED
    assert event.provider_raw_status == "authorized"


def test_parse_state_instance_changed_unknown_state_maps_to_unknown():
    payload = _base(
        "stateInstanceChanged",
        instanceData={"idInstance": 123, "stateInstance": "brandNew"},
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderConnectionStateChanged)
    assert event.status is ConnectionStatus.UNKNOWN
    assert event.provider_raw_status == "brandNew"


def test_parse_unknown_type_webhook_returns_none():
    payload = _base("pollMessageReceived")
    assert GreenEventAdapter().parse(payload) is None


def test_parse_non_dict_returns_none():
    assert GreenEventAdapter().parse("not a dict") is None  # type: ignore[arg-type]
    assert GreenEventAdapter().parse(None) is None  # type: ignore[arg-type]


def test_parse_missing_type_webhook_returns_none():
    payload = {"instanceData": {"idInstance": 123}}
    assert GreenEventAdapter().parse(payload) is None


def test_parse_missing_instance_data_returns_none():
    assert GreenEventAdapter().parse({"typeWebhook": "incomingMessageReceived"}) is None


def test_parse_missing_id_instance_returns_none():
    payload = {"typeWebhook": "incomingMessageReceived", "instanceData": {}}
    assert GreenEventAdapter().parse(payload) is None


def test_parse_unknown_type_message_maps_to_other_kind():
    payload = _base(
        "incomingMessageReceived",
        chatId="c",
        idMessage="m",
        messageData={"typeMessage": "stickerMessage"},
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.kind is MessageKind.OTHER


def test_parse_reaction_message_maps_to_reaction_kind():
    payload = _base(
        "incomingMessageReceived",
        chatId="c",
        idMessage="m",
        messageData={"typeMessage": "reactionMessage", "reactionText": "👍"},
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.kind is MessageKind.REACTION


def test_parse_image_message_with_caption():
    payload = _base(
        "incomingMessageReceived",
        chatId="c",
        idMessage="m",
        messageData={"typeMessage": "imageMessage", "caption": "look at this"},
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.kind is MessageKind.IMAGE
    assert event.text == "look at this"


def test_parse_quoted_message_extracts_text():
    payload = _base(
        "incomingMessageReceived",
        chatId="c",
        idMessage="m",
        messageData={"typeMessage": "quotedMessage", "textMessage": "reply text"},
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.kind is MessageKind.TEXT
    assert event.text == "reply text"


def test_parse_timestamp_millis_converted_to_utc():
    payload = _base(
        "incomingMessageReceived",
        chatId="c",
        idMessage="m",
        timestamp=1700000000000,  # millis
        messageData={"typeMessage": "textMessage", "textMessage": "x"},
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.timestamp == datetime.fromtimestamp(1700000000, timezone.utc)
    assert event.timestamp.tzinfo is timezone.utc


def test_parse_invalid_timestamp_falls_back_to_now_utc():
    payload = _base(
        "incomingMessageReceived",
        chatId="c",
        idMessage="m",
        timestamp="not-a-number",
        messageData={"typeMessage": "textMessage", "textMessage": "x"},
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.timestamp.tzinfo is timezone.utc


def test_parse_missing_timestamp_falls_back_to_now_utc():
    payload = {
        "typeWebhook": "incomingMessageReceived",
        "instanceData": {"idInstance": 123},
        "chatId": "c",
        "idMessage": "m",
        "messageData": {"typeMessage": "textMessage", "textMessage": "x"},
    }
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.timestamp.tzinfo is timezone.utc


def test_parse_message_missing_chat_id_returns_none():
    payload = _base(
        "incomingMessageReceived",
        idMessage="m",
        messageData={"typeMessage": "textMessage", "textMessage": "x"},
    )
    assert GreenEventAdapter().parse(payload) is None


def test_parse_message_missing_id_message_returns_none():
    payload = _base(
        "incomingMessageReceived",
        chatId="c",
        messageData={"typeMessage": "textMessage", "textMessage": "x"},
    )
    assert GreenEventAdapter().parse(payload) is None


def test_parse_message_missing_message_data_returns_none():
    payload = _base("incomingMessageReceived", chatId="c", idMessage="m")
    assert GreenEventAdapter().parse(payload) is None


def test_parse_status_missing_id_message_returns_none():
    payload = _base("outgoingMessageStatus", messageData={"statusWebhook": "delivered"})
    assert GreenEventAdapter().parse(payload) is None


def test_parse_status_missing_status_returns_none():
    payload = _base("outgoingMessageStatus", idMessage="m", messageData={})
    assert GreenEventAdapter().parse(payload) is None


def test_parse_state_missing_instance_data_returns_none():
    payload = {"typeWebhook": "stateInstanceChanged", "timestamp": 1700000000}
    assert GreenEventAdapter().parse(payload) is None


def test_parse_id_instance_as_int_is_stringified():
    payload = _base(
        "incomingMessageReceived",
        chatId="c",
        idMessage="m",
        messageData={"typeMessage": "textMessage", "textMessage": "x"},
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.connection.provider_connection_id == "123"


def test_event_has_no_raw_field():
    import dataclasses

    for cls in (
        ProviderMessageEvent,
        ProviderMessageStatusEvent,
        ProviderConnectionStateChanged,
    ):
        fields = {f.name for f in dataclasses.fields(cls)}
        assert "raw" not in fields
        assert "user_id" not in fields


def test_parse_status_with_non_dict_message_data_returns_none():
    payload = _base("outgoingMessageStatus", idMessage="m", messageData="not-a-dict")
    assert GreenEventAdapter().parse(payload) is None


def test_parse_state_with_non_dict_instance_data_returns_none():
    # _connection_ref validates instanceData before _state_event is reached,
    # so a non-dict instanceData is rejected at the parse() level.
    payload = {
        "typeWebhook": "stateInstanceChanged",
        "instanceData": "not-a-dict",
        "timestamp": 1700000000,
    }
    assert GreenEventAdapter().parse(payload) is None


def test_parse_extended_text_without_text_falls_back_to_text_message():
    payload = _base(
        "incomingMessageReceived",
        chatId="c",
        idMessage="m",
        messageData={
            "typeMessage": "extendedTextMessage",
            "extendedTextMessage": {"description": "no text field"},
            "textMessage": "fallback",
        },
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.text == "fallback"


def test_parse_extended_text_without_text_or_fallback_returns_none_text():
    payload = _base(
        "incomingMessageReceived",
        chatId="c",
        idMessage="m",
        messageData={
            "typeMessage": "extendedTextMessage",
            "extendedTextMessage": {"description": "no text"},
        },
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.text is None


def test_parse_extended_text_with_non_dict_ext_falls_back_to_text_message():
    payload = _base(
        "incomingMessageReceived",
        chatId="c",
        idMessage="m",
        messageData={
            "typeMessage": "extendedTextMessage",
            "extendedTextMessage": "not-a-dict",
            "textMessage": "fallback",
        },
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.text == "fallback"


def test_parse_non_text_message_without_caption_returns_none_text():
    payload = _base(
        "incomingMessageReceived",
        chatId="c",
        idMessage="m",
        messageData={"typeMessage": "imageMessage"},  # no caption
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.text is None


def test_parse_status_with_status_in_payload_root_not_message_data():
    payload = _base(
        "outgoingMessageStatus",
        idMessage="m",
        status="read",  # root-level status fallback
        messageData={},
    )
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageStatusEvent)
    assert event.status == "read"


def test_parse_message_with_chat_id_in_message_data_when_root_missing():
    payload = {
        "typeWebhook": "incomingMessageReceived",
        "instanceData": {"idInstance": 123},
        "idMessage": "m",
        "timestamp": 1700000000,
        "messageData": {
            "typeMessage": "textMessage",
            "textMessage": "x",
            "chatId": "from-md@c.us",
        },
    }
    event = GreenEventAdapter().parse(payload)
    assert isinstance(event, ProviderMessageEvent)
    assert event.chat_id == "from-md@c.us"
