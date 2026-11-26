"""Tests for the GOWS envelope parser (adapter boundary).

Uses envelope shapes captured from the real canary batches (Nov 2026):
Go-style top-level keys (``Info``/``Message``), protobuf-style inner keys
(``conversation``, ``audioMessage``), LID chats, PTT voice notes, and
group messages.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from echo_v2.domain.waha import WahaInboxMessage
from echo_v2.ports.whatsapp import MessageDirection, MessageKind, MessageSource
from echo_v2.services.waha_messages import extract_media, parse_envelope

NOW = datetime(2026, 11, 26, 22, 5, 12, tzinfo=timezone.utc)


def _row(data: dict[str, Any], *, session: str = "gaddy-test", message_id: str = "M-1") -> WahaInboxMessage:
    return WahaInboxMessage(
        session=session,
        message_id=message_id,
        chat_jid="61280626974807@lid",
        whatsapp_timestamp=NOW,
        from_me=1,
        is_real=1,
        data=json.dumps(data),
        sha256="0" * 64,
        seq=5,
        agent_first_seen_at=NOW,
        batch_id="b-1",
    )


def _info(**overrides: Any) -> dict[str, Any]:
    info = {
        "Chat": "61280626974807@lid",
        "Sender": "61280626974807@lid",
        "IsFromMe": False,
        "IsGroup": False,
        "ID": "M-1",
        "Type": "text",
        "PushName": "גדי",
    }
    info.update(overrides)
    return info


def test_parses_incoming_text_message() -> None:
    row = _row({"Info": _info(), "Message": {"conversation": "נכנסתי למיטה"}})
    event = parse_envelope(row)
    assert event.connection.provider == "waha"
    assert event.connection.provider_connection_id == "gaddy-test"
    assert event.chat_id == "61280626974807@lid"
    assert event.provider_message_id == "M-1"
    assert event.direction is MessageDirection.INBOUND
    assert event.source is None
    assert event.kind is MessageKind.TEXT
    assert event.text == "נכנסתי למיטה"
    assert event.sender_id == "61280626974807@lid"
    assert event.sender_name == "גדי"
    assert event.is_group is False
    assert event.media_reference is None
    assert event.timestamp == NOW
    assert event.event_id == "waha:gaddy-test:M-1"


def test_parses_outgoing_group_text() -> None:
    row = _row(
        {
            "Info": _info(
                Chat="972546610653-1492611986@g.us",
                Sender="21758421798914@lid",
                IsFromMe=True,
                IsGroup=True,
            ),
            "Message": {"conversation": "הי הי"},
        }
    )
    event = parse_envelope(row)
    assert event.direction is MessageDirection.OUTBOUND
    assert event.source is MessageSource.USER
    assert event.is_group is True
    assert event.chat_id.endswith("@g.us")


def test_parses_extended_text() -> None:
    row = _row(
        {
            "Info": _info(),
            "Message": {"extendedTextMessage": {"text": "see https://example.com"}},
        }
    )
    event = parse_envelope(row)
    assert event.kind is MessageKind.TEXT
    assert event.text == "see https://example.com"


def test_parses_ptt_audio_with_media_reference() -> None:
    row = _row(
        {
            "Info": _info(IsFromMe=True, Type="media"),
            "Message": {
                "audioMessage": {
                    "URL": "https://mmg.whatsapp.net/v/t62.7117-24/fake",
                    "mediaKey": "bWVkaWFLZXk=",
                    "fileLength": "7023",
                    "fileSHA256": "c2hhMjU2=",
                    "mimetype": "audio/ogg; codecs=opus",
                    "seconds": 3,
                    "PTT": True,
                }
            },
        }
    )
    event = parse_envelope(row)
    assert event.kind is MessageKind.AUDIO
    assert event.text is None  # no caption on audio
    assert event.media_reference == "waha:gaddy-test:M-1"
    assert event.media_download_url is None
    assert event.media_mime_type == "audio/ogg; codecs=opus"
    assert event.media_size == 7023

    media = extract_media(row)
    assert media is not None
    assert media.media_type == "audio"
    assert media.url == "https://mmg.whatsapp.net/v/t62.7117-24/fake"
    assert media.media_key == "bWVkaWFLZXk="
    assert media.file_length == 7023  # string in the envelope, parsed to int
    assert media.file_sha256 == "c2hhMjU2="


def test_parses_image_with_caption_as_text() -> None:
    row = _row(
        {
            "Info": _info(Type="media"),
            "Message": {
                "imageMessage": {
                    "URL": "https://mmg.whatsapp.net/v/img",
                    "mediaKey": "a2V5",
                    "fileLength": 1234,
                    "fileSHA256": "aGFzaA==",
                    "mimetype": "image/jpeg",
                    "caption": "look at this",
                }
            },
        }
    )
    event = parse_envelope(row)
    assert event.kind is MessageKind.IMAGE
    assert event.text == "look at this"
    assert event.media_reference == "waha:gaddy-test:M-1"


def test_parses_document_with_file_name() -> None:
    row = _row(
        {
            "Info": _info(Type="media"),
            "Message": {
                "documentMessage": {
                    "URL": "https://mmg.whatsapp.net/v/doc",
                    "mediaKey": "a2V5",
                    "fileLength": 99,
                    "fileSHA256": "aGFzaA==",
                    "mimetype": "application/pdf",
                    "fileName": "report.pdf",
                }
            },
        }
    )
    event = parse_envelope(row)
    assert event.kind is MessageKind.DOCUMENT
    assert event.media_file_name == "report.pdf"
    assert event.media_size == 99


def test_message_null_maps_to_other() -> None:
    row = _row({"Info": _info(), "Message": None})
    event = parse_envelope(row)
    assert event.kind is MessageKind.OTHER
    assert event.text is None
    assert event.media_reference is None
    assert extract_media(row) is None


def test_media_without_url_or_key_is_not_media() -> None:
    row = _row(
        {
            "Info": _info(Type="media"),
            "Message": {
                "audioMessage": {"mimetype": "audio/ogg", "seconds": 2}
            },
        }
    )
    event = parse_envelope(row)
    assert event.kind is MessageKind.AUDIO
    assert event.media_reference is None  # fundamentally unavailable


def test_invalid_json_raises() -> None:
    row = _row({"Info": _info()})
    object.__setattr__(row, "data", "{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_envelope(row)


def test_non_object_envelope_raises() -> None:
    row = _row({"Info": _info()})
    object.__setattr__(row, "data", "[1, 2]")
    with pytest.raises(TypeError, match="JSON object"):
        parse_envelope(row)


def test_missing_info_raises() -> None:
    row = _row({"Message": {"conversation": "hi"}})
    with pytest.raises(TypeError, match="Info"):
        parse_envelope(row)


def test_extract_media_returns_none_on_bad_json() -> None:
    row = _row({"Info": _info()})
    object.__setattr__(row, "data", "not json")
    assert extract_media(row) is None
