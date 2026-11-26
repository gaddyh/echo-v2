"""Additional branch coverage for the GOWS envelope parser."""

from __future__ import annotations

import pytest

from echo_v2.ports.whatsapp import MessageKind
from echo_v2.services.waha_messages import extract_media, parse_envelope
from tests.services.test_waha_messages import _info, _row


def test_reaction_message_maps_to_reaction_kind() -> None:
    row = _row(
        {
            "Info": _info(),
            "Message": {"reactionMessage": {"text": "👍"}},
        }
    )
    assert parse_envelope(row).kind is MessageKind.REACTION


def test_unknown_message_content_maps_to_other() -> None:
    row = _row({"Info": _info(), "Message": {"pollMessage": {"name": "q"}}})
    assert parse_envelope(row).kind is MessageKind.OTHER


def test_non_dict_message_raises() -> None:
    row = _row({"Info": _info(), "Message": "nope"})
    with pytest.raises(TypeError, match="invalid Message"):
        parse_envelope(row)


def test_missing_chat_raises() -> None:
    row = _row(
        {"Info": _info(Chat=""), "Message": {"conversation": "hi"}}
    )
    with pytest.raises(ValueError, match="Info.Chat"):
        parse_envelope(row)


def test_missing_is_from_me_raises() -> None:
    row = _row(
        {
            "Info": _info(IsFromMe=None),
            "Message": {"conversation": "hi"},
        }
    )
    with pytest.raises(ValueError, match="Info.IsFromMe"):
        parse_envelope(row)


def test_invalid_sender_raises() -> None:
    row = _row(
        {"Info": _info(Sender=123), "Message": {"conversation": "hi"}}
    )
    with pytest.raises(ValueError, match="Info.Sender"):
        parse_envelope(row)


def test_invalid_push_name_raises() -> None:
    row = _row(
        {"Info": _info(PushName=123), "Message": {"conversation": "hi"}}
    )
    with pytest.raises(ValueError, match="Info.PushName"):
        parse_envelope(row)


def test_media_with_url_but_no_key_is_not_media() -> None:
    row = _row(
        {
            "Info": _info(),
            "Message": {
                "imageMessage": {
                    "URL": "https://mmg.whatsapp.net/v/img",
                    "mimetype": "image/jpeg",
                }
            },
        }
    )
    assert extract_media(row) is None
    assert parse_envelope(row).media_reference is None


def test_media_with_garbage_length_is_not_media() -> None:
    row = _row(
        {
            "Info": _info(),
            "Message": {
                "imageMessage": {
                    "URL": "https://mmg.whatsapp.net/v/img",
                    "mediaKey": "a2V5",
                    "fileLength": "seventy",
                    "fileSHA256": "aGFzaA==",
                }
            },
        }
    )
    assert extract_media(row) is None


def test_media_with_bad_length_or_hash_is_not_media() -> None:
    row = _row(
        {
            "Info": _info(),
            "Message": {
                "imageMessage": {
                    "URL": "https://mmg.whatsapp.net/v/img",
                    "mediaKey": "a2V5",
                    "fileLength": [1],
                    "fileSHA256": "aGFzaA==",
                }
            },
        }
    )
    assert extract_media(row) is None


def test_sticker_maps_to_other_kind_with_media() -> None:
    row = _row(
        {
            "Info": _info(),
            "Message": {
                "stickerMessage": {
                    "URL": "https://mmg.whatsapp.net/v/st",
                    "mediaKey": "a2V5",
                    "fileLength": 5,
                    "fileSHA256": "aGFzaA==",
                    "mimetype": "image/webp",
                }
            },
        }
    )
    event = parse_envelope(row)
    assert event.kind is MessageKind.OTHER
    assert event.media_reference == "waha:gaddy-test:M-1"
    assert extract_media(row).media_type == "sticker"


def test_empty_caption_is_not_text() -> None:
    row = _row(
        {
            "Info": _info(),
            "Message": {
                "imageMessage": {
                    "URL": "https://mmg.whatsapp.net/v/img",
                    "mediaKey": "a2V5",
                    "fileLength": 5,
                    "fileSHA256": "aGFzaA==",
                    "caption": "",
                }
            },
        }
    )
    assert parse_envelope(row).text is None
