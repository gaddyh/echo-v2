"""Tests for the 360dialog event adapter (webhook → BotEvent)."""

from __future__ import annotations

from echo_v2.integrations.dialog360.events import Dialog360EventAdapter
from echo_v2.ports.bot import BotEventAdapter, BotEventType


def test_adapter_satisfies_port():
    assert isinstance(Dialog360EventAdapter(), BotEventAdapter)


def test_parse_text_message():
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.HBHKGUY",
                        "from": "972500000001",
                        "type": "text",
                        "text": {"body": "מחר ב-8"},
                        "timestamp": "1700000000",
                    }],
                    "contacts": [
                        {"profile": {"name": "Gaddy"}, "wa_id": "972500000001"}
                    ],
                }
            }]
        }]
    }
    event = adapter.parse(payload)
    assert event is not None
    assert event.event_id == "wamid.HBHKGUY"
    assert event.user_phone == "972500000001"
    assert event.type is BotEventType.TEXT
    assert event.text == "מחר ב-8"
    assert event.timestamp is not None


def test_parse_contact_message():
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.CONTACT1",
                        "from": "972500000001",
                        "type": "contacts",
                        "contacts": [{
                            "name": {
                                "formatted_name": "Dana Cohen",
                                "first_name": "Dana",
                                "last_name": "Cohen",
                            },
                            "phones": [
                                {"wa_id": "972526610653", "phone": "+972 52-661-0653"}
                            ],
                        }],
                        "timestamp": "1700000001",
                    }],
                    "contacts": [
                        {"profile": {"name": "Gaddy"}, "wa_id": "972500000001"}
                    ],
                }
            }]
        }]
    }
    event = adapter.parse(payload)
    assert event is not None
    assert event.type is BotEventType.CONTACT
    assert event.contact is not None
    assert event.contact.phone == "972526610653"
    assert event.contact.name == "Dana Cohen"


def test_parse_contact_prefers_wa_id_over_phone():
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.C2",
                        "from": "972500000001",
                        "type": "contacts",
                        "contacts": [{
                            "name": {"formatted_name": "Bob"},
                            "phones": [
                                {"phone": "+1 555-1234"},
                                {"wa_id": "15551234567"},
                            ],
                        }],
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    event = adapter.parse(payload)
    assert event is not None
    assert event.contact.phone == "15551234567"


def test_parse_contact_no_phones_returns_none():
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.C3",
                        "from": "972500000001",
                        "type": "contacts",
                        "contacts": [{
                            "name": {"formatted_name": "Empty"},
                            "phones": [],
                        }],
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    assert adapter.parse(payload) is None


def test_parse_status_update_returns_none():
    """Status updates (delivery receipts) are not user messages."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "statuses": [{
                        "id": "wamid.S1",
                        "status": "delivered",
                    }],
                }
            }]
        }]
    }
    assert adapter.parse(payload) is None


def test_parse_empty_payload_returns_none():
    adapter = Dialog360EventAdapter()
    assert adapter.parse({}) is None
    assert adapter.parse(None) is None  # type: ignore[arg-type]


def test_parse_unknown_message_type_returns_none():
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.X",
                        "from": "972500000001",
                        "type": "sticker",
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    assert adapter.parse(payload) is None


def test_parse_missing_message_id_returns_none():
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": "972500000001",
                        "type": "text",
                        "text": {"body": "hi"},
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    assert adapter.parse(payload) is None


def test_parse_falls_back_to_message_from_for_phone():
    """If contacts[] is missing, use message.from as the sender phone."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.F1",
                        "from": "972500000099",
                        "type": "text",
                        "text": {"body": "hello"},
                    }],
                }
            }]
        }]
    }
    event = adapter.parse(payload)
    assert event is not None
    assert event.user_phone == "972500000099"


def test_parse_button_reply():
    """Quick Reply button replies should be parsed as TEXT events."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.BUTTON1",
                        "from": "972500000001",
                        "type": "button",
                        "button": {"text": "צפה בשיחות"},
                        "timestamp": "1700000000",
                    }],
                    "contacts": [
                        {"profile": {"name": "Gaddy"}, "wa_id": "972500000001"}
                    ],
                }
            }]
        }]
    }
    event = adapter.parse(payload)
    assert event is not None
    assert event.event_id == "wamid.BUTTON1"
    assert event.user_phone == "972500000001"
    assert event.type is BotEventType.TEXT
    assert event.text == "צפה בשיחות"
    assert event.timestamp is not None


def test_parse_button_reply_no_text_returns_none():
    """A button reply without text should return None."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.BUTTON2",
                        "from": "972500000001",
                        "type": "button",
                        "button": {},
                        "timestamp": "1700000000",
                    }],
                    "contacts": [
                        {"profile": {"name": "Gaddy"}, "wa_id": "972500000001"}
                    ],
                }
            }]
        }]
    }
    event = adapter.parse(payload)
    assert event is None


def test_parse_interactive_button_reply():
    """Interactive button_reply should be parsed as BUTTON_REPLY event."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.IR1",
                        "from": "972500000001",
                        "type": "interactive",
                        "interactive": {
                            "type": "button_reply",
                            "button_reply": {
                                "id": "action:active-1:acknowledge",
                                "title": "מטפל עכשיו",
                            },
                        },
                        "timestamp": "1700000000",
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    event = adapter.parse(payload)
    assert event is not None
    assert event.type is BotEventType.BUTTON_REPLY
    assert event.button_id == "action:active-1:acknowledge"
    assert event.text == "מטפל עכשיו"


def test_parse_interactive_button_reply_no_id_returns_none():
    """Interactive button_reply without id returns None."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.IR2",
                        "from": "972500000001",
                        "type": "interactive",
                        "interactive": {
                            "type": "button_reply",
                            "button_reply": {"title": "no id"},
                        },
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    assert adapter.parse(payload) is None


def test_parse_interactive_list_reply():
    """Interactive list_reply should be parsed as LIST_REPLY event."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.LR1",
                        "from": "972500000001",
                        "type": "interactive",
                        "interactive": {
                            "type": "list_reply",
                            "list_reply": {
                                "id": "list-item-1",
                                "title": "פריט 1",
                            },
                        },
                        "timestamp": "1700000000",
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    event = adapter.parse(payload)
    assert event is not None
    assert event.type is BotEventType.LIST_REPLY
    assert event.list_id == "list-item-1"
    assert event.text == "פריט 1"


def test_parse_interactive_list_reply_no_id_returns_none():
    """Interactive list_reply without id returns None."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.LR2",
                        "from": "972500000001",
                        "type": "interactive",
                        "interactive": {
                            "type": "list_reply",
                            "list_reply": {"title": "no id"},
                        },
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    assert adapter.parse(payload) is None


def test_parse_interactive_not_dict_returns_none():
    """Interactive field that isn't a dict returns None."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.IR3",
                        "from": "972500000001",
                        "type": "interactive",
                        "interactive": "not-a-dict",
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    assert adapter.parse(payload) is None


def test_parse_interactive_unknown_type_returns_none():
    """Interactive with unknown type returns None."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.IR4",
                        "from": "972500000001",
                        "type": "interactive",
                        "interactive": {"type": "nfc_reply"},
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    assert adapter.parse(payload) is None


def test_parse_text_empty_body_returns_none():
    """Text message with empty body returns None."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.T1",
                        "from": "972500000001",
                        "type": "text",
                        "text": {"body": ""},
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    assert adapter.parse(payload) is None


def test_parse_text_body_not_dict():
    """Text message where text field isn't a dict returns None."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.T2",
                        "from": "972500000001",
                        "type": "text",
                        "text": "not-a-dict",
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    assert adapter.parse(payload) is None


def test_parse_no_phone_returns_none():
    """Message with no phone (contacts empty, no message.from) returns None."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.NP1",
                        "type": "text",
                        "text": {"body": "hi"},
                    }],
                    "contacts": [],
                }
            }]
        }]
    }
    assert adapter.parse(payload) is None


def test_parse_message_not_dict_returns_none():
    """Message that isn't a dict returns None."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": ["not-a-dict"],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    assert adapter.parse(payload) is None


def test_parse_contacts_not_list_returns_none():
    """Contacts field that isn't a list returns None for contact message."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.C4",
                        "from": "972500000001",
                        "type": "contacts",
                        "contacts": "not-a-list",
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    assert adapter.parse(payload) is None


def test_parse_contact_name_from_first_last_only():
    """Contact name falls back to first+last when no formatted_name."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.C5",
                        "from": "972500000001",
                        "type": "contacts",
                        "contacts": [{
                            "name": {
                                "first_name": "John",
                                "last_name": "Doe",
                            },
                            "phones": [{"wa_id": "15551234567"}],
                        }],
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    event = adapter.parse(payload)
    assert event is not None
    assert event.contact.name == "John Doe"


def test_parse_contact_name_not_dict():
    """Contact name field that isn't a dict defaults to empty string."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.C6",
                        "from": "972500000001",
                        "type": "contacts",
                        "contacts": [{
                            "name": "not-a-dict",
                            "phones": [{"wa_id": "15551234567"}],
                        }],
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    event = adapter.parse(payload)
    assert event is not None
    assert event.contact.name == ""


def test_parse_contact_phone_fallback_to_phone_field():
    """Contact phone falls back to phone field when no wa_id."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.C7",
                        "from": "972500000001",
                        "type": "contacts",
                        "contacts": [{
                            "name": {"formatted_name": "Test"},
                            "phones": [{"phone": "+1 555-9999"}],
                        }],
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    event = adapter.parse(payload)
    assert event is not None
    assert event.contact.phone == "+1 555-9999"


def test_parse_contact_phone_obj_not_dict_skipped():
    """Non-dict phone objects are skipped during phone extraction."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.C8",
                        "from": "972500000001",
                        "type": "contacts",
                        "contacts": [{
                            "name": {"formatted_name": "Test"},
                            "phones": ["not-a-dict", {"wa_id": "15551234567"}],
                        }],
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    event = adapter.parse(payload)
    assert event is not None
    assert event.contact.phone == "15551234567"


def test_parse_invalid_timestamp_returns_none_timestamp():
    """Invalid timestamp returns event with None timestamp."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.TS1",
                        "from": "972500000001",
                        "type": "text",
                        "text": {"body": "hi"},
                        "timestamp": "not-a-number",
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    event = adapter.parse(payload)
    assert event is not None
    assert event.timestamp is None


def test_parse_no_timestamp_returns_none_timestamp():
    """Missing timestamp returns event with None timestamp."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.TS2",
                        "from": "972500000001",
                        "type": "text",
                        "text": {"body": "hi"},
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    event = adapter.parse(payload)
    assert event is not None
    assert event.timestamp is None


def test_parse_entry_not_list_returns_none():
    """Entry field that isn't a list returns None."""
    adapter = Dialog360EventAdapter()
    payload = {"entry": "not-a-list"}
    assert adapter.parse(payload) is None


def test_parse_changes_not_list_returns_none():
    """Changes field that isn't a list returns None."""
    adapter = Dialog360EventAdapter()
    payload = {"entry": [{"changes": "not-a-list"}]}
    assert adapter.parse(payload) is None


def test_parse_value_not_dict_returns_none():
    """Value field that isn't a dict returns None."""
    adapter = Dialog360EventAdapter()
    payload = {"entry": [{"changes": [{"value": "not-a-dict"}]}]}
    assert adapter.parse(payload) is None


def test_parse_empty_entry_returns_none():
    """Empty entry list returns None."""
    adapter = Dialog360EventAdapter()
    payload = {"entry": []}
    assert adapter.parse(payload) is None


def test_parse_messages_not_list_returns_none():
    """Messages field that isn't a list returns None."""
    adapter = Dialog360EventAdapter()
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": "not-a-list",
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }
    assert adapter.parse(payload) is None
