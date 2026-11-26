"""Tests for WAHA media decryption and the file:// media resolver.

The decryptor round-trip encrypts with the same protocol (HKDF → AES-CBC +
trailing MAC → declared length/hash) and verifies the port of the proven
``decrypt_media.py``: correct plaintext recovery, truncation to
``fileLength``, SHA-256 gating, and the HKDF implementation matching
:mod:`cryptography`'s HKDF.

The resolver tests fake the inbox and stub the CDN download.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from echo_v2.domain.waha import WahaInboxMessage
from echo_v2.services import waha_media
from echo_v2.services.waha_media import (
    WahaMediaDecryptor,
    WahaMediaError,
    WahaMediaUrlResolver,
)

_INFO_STRINGS = {
    "audio": "WhatsApp Audio Keys",
    "image": "WhatsApp Image Keys",
    "video": "WhatsApp Video Keys",
    "document": "WhatsApp Document Keys",
}


def _hkdf(ikm: bytes, info: str, length: int = 112) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=length, salt=None, info=info.encode()
    ).derive(ikm)


def _encrypt_media(
    plaintext: bytes, media_type: str = "audio"
) -> tuple[bytes, str, str, int]:
    """Produce (ciphertext_with_mac, media_key_b64, file_sha256_b64, file_length)."""
    media_key = b"k" * 32
    keys = _hkdf(media_key, _INFO_STRINGS[media_type])
    iv, cipher_key, mac_key = keys[:16], keys[16:48], keys[48:80]
    # WhatsApp pads the plaintext to the AES block size before encryption;
    # the declared fileLength trims the padding back off.
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + b"\x01" * pad_len
    encryptor = Cipher(algorithms.AES(cipher_key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    mac = hmac.new(mac_key, ciphertext, hashlib.sha256).digest()[:10]
    return (
        ciphertext + mac,
        base64.b64encode(media_key).decode(),
        base64.b64encode(hashlib.sha256(plaintext).digest()).decode(),
        len(plaintext),
    )


PLAINTEXT = b"OggS-fake-voice-note-payload" * 40


def test_hkdf_matches_cryptography_library() -> None:
    ikm = b"\x11" * 32
    assert WahaMediaDecryptor._hkdf_sha256(ikm, "WhatsApp Audio Keys", 112) == _hkdf(
        ikm, "WhatsApp Audio Keys"
    )


def test_decrypt_round_trip_recovers_plaintext() -> None:
    ciphertext, media_key, file_sha256, file_length = _encrypt_media(PLAINTEXT)
    decrypted = WahaMediaDecryptor().decrypt(
        ciphertext=ciphertext,
        media_key=media_key,
        media_type="audio",
        file_length=file_length,
        file_sha256=file_sha256,
    )
    assert decrypted == PLAINTEXT


@pytest.mark.parametrize("media_type", ["image", "video", "document"])
def test_decrypt_supports_other_media_types(media_type: str) -> None:
    ciphertext, media_key, file_sha256, file_length = _encrypt_media(
        PLAINTEXT[:64], media_type=media_type
    )
    decrypted = WahaMediaDecryptor().decrypt(
        ciphertext=ciphertext,
        media_key=media_key,
        media_type=media_type,
        file_length=file_length,
        file_sha256=file_sha256,
    )
    assert decrypted == PLAINTEXT[:64]


def test_decrypt_truncates_to_declared_length() -> None:
    # Declare a shorter length than the plaintext: the protocol truncates.
    ciphertext, media_key, _file_sha256, _length = _encrypt_media(PLAINTEXT)
    decrypted = WahaMediaDecryptor().decrypt(
        ciphertext=ciphertext,
        media_key=media_key,
        media_type="audio",
        file_length=100,
        file_sha256=base64.b64encode(hashlib.sha256(PLAINTEXT[:100]).digest()).decode(),
    )
    assert decrypted == PLAINTEXT[:100]


def test_decrypt_rejects_sha_mismatch() -> None:
    ciphertext, media_key, _file_sha256, file_length = _encrypt_media(PLAINTEXT)
    with pytest.raises(WahaMediaError, match="SHA-256 mismatch"):
        WahaMediaDecryptor().decrypt(
            ciphertext=ciphertext,
            media_key=media_key,
            media_type="audio",
            file_length=file_length,
            file_sha256=base64.b64encode(b"\x00" * 32).decode(),
        )


def test_decrypt_rejects_unsupported_media_type() -> None:
    ciphertext, media_key, file_sha256, file_length = _encrypt_media(PLAINTEXT)
    with pytest.raises(WahaMediaError, match="unsupported media type"):
        WahaMediaDecryptor().decrypt(
            ciphertext=ciphertext,
            media_key=media_key,
            media_type="hologram",
            file_length=file_length,
            file_sha256=file_sha256,
        )


def test_decrypt_rejects_short_ciphertext() -> None:
    with pytest.raises(WahaMediaError, match="shorter than declared"):
        WahaMediaDecryptor().decrypt(
            ciphertext=b"x" * 42,
            media_key=base64.b64encode(b"k" * 32).decode(),
            media_type="audio",
            file_length=10_000,
            file_sha256=base64.b64encode(b"\x00" * 32).decode(),
        )


def test_decrypt_rejects_unaligned_ciphertext() -> None:
    with pytest.raises(WahaMediaError, match="not block-aligned"):
        WahaMediaDecryptor().decrypt(
            ciphertext=b"x" * 32,
            media_key=base64.b64encode(b"k" * 32).decode(),
            media_type="audio",
            file_length=16,
            file_sha256=base64.b64encode(b"\x00" * 32).decode(),
        )


def test_decrypt_rejects_invalid_base64() -> None:
    with pytest.raises(WahaMediaError, match="invalid base64"):
        WahaMediaDecryptor().decrypt(
            ciphertext=b"x" * 32,
            media_key="!!!not-base64!!!",
            media_type="audio",
            file_length=16,
            file_sha256=base64.b64encode(b"\x00" * 32).decode(),
        )


# --- resolver -----------------------------------------------------------------


def _inbox_message(data: str) -> WahaInboxMessage:
    return WahaInboxMessage(
        session="gaddy-test",
        message_id="AUDIO-1",
        chat_jid="61280626974807@lid",
        whatsapp_timestamp=datetime(2026, 11, 26, 22, 5, 12, tzinfo=timezone.utc),
        from_me=1,
        is_real=1,
        data=data,
        sha256="0" * 64,
        seq=5,
        agent_first_seen_at=datetime(2026, 11, 26, 22, 5, 12, tzinfo=timezone.utc),
        batch_id="b-1",
    )


def _audio_row() -> WahaInboxMessage:
    _ciphertext, media_key, file_sha256, file_length = _encrypt_media(PLAINTEXT)
    envelope = {
        "Info": {"Chat": "61280626974807@lid", "IsFromMe": True, "Type": "media"},
        "Message": {
            "audioMessage": {
                "URL": "https://mmg.whatsapp.net/v/t62.7117-24/fake",
                "mediaKey": media_key,
                "fileLength": file_length,
                "fileSHA256": file_sha256,
                "mimetype": "audio/ogg; codecs=opus",
                "seconds": 3,
                "PTT": True,
            }
        },
    }
    return _inbox_message(json.dumps(envelope))


class FakeInbox:
    def __init__(self, row: WahaInboxMessage | None) -> None:
        self.row = row

    async def get(self, session: str, message_id: str) -> WahaInboxMessage | None:
        return self.row


def _domain_message(reference: str) -> SimpleNamespace:
    return SimpleNamespace(media_reference=reference)


async def test_resolver_decrypts_and_returns_file_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _audio_row()
    ciphertext, _mk, _fs, _fl = _encrypt_media(PLAINTEXT)
    monkeypatch.setattr(
        waha_media, "download_ciphertext", _fake_download(ciphertext)
    )
    resolver = WahaMediaUrlResolver(
        FakeInbox(row), cache_dir=tmp_path / "media"
    )
    url = await resolver.resolve(_domain_message("waha:gaddy-test:AUDIO-1"))
    assert url is not None and url.startswith("file://")
    content = Path(url[len("file://"):]).read_bytes()
    assert content == PLAINTEXT

    # Second resolve is served from the content-addressed cache.
    called = []

    async def failing_download(url: str, *, max_bytes: int) -> bytes:
        called.append(url)
        raise AssertionError("should not download again")

    monkeypatch.setattr(waha_media, "download_ciphertext", failing_download)
    url2 = await resolver.resolve(_domain_message("waha:gaddy-test:AUDIO-1"))
    assert url2 == url
    assert called == []


def _fake_download(ciphertext: bytes):
    async def download(url: str, *, max_bytes: int) -> bytes:
        assert url == "https://mmg.whatsapp.net/v/t62.7117-24/fake"
        assert max_bytes == 16 * 1024 * 1024
        return ciphertext

    return download


async def test_resolver_ignores_non_waha_references(tmp_path: Path) -> None:
    resolver = WahaMediaUrlResolver(FakeInbox(None), cache_dir=tmp_path / "media")
    assert await resolver.resolve(_domain_message("baileys:conn:ref")) is None
    assert await resolver.resolve(_domain_message("")) is None


async def test_resolver_raises_when_row_missing(tmp_path: Path) -> None:
    resolver = WahaMediaUrlResolver(FakeInbox(None), cache_dir=tmp_path / "media")
    with pytest.raises(WahaMediaError, match="not found"):
        await resolver.resolve(_domain_message("waha:gaddy-test:GONE"))


async def test_resolver_returns_none_when_envelope_has_no_media(
    tmp_path: Path,
) -> None:
    row = _inbox_message(
        json.dumps({"Info": {"Chat": "x@lid", "IsFromMe": False}, "Message": None})
    )
    resolver = WahaMediaUrlResolver(FakeInbox(row), cache_dir=tmp_path / "media")
    assert await resolver.resolve(_domain_message("waha:gaddy-test:AUDIO-1")) is None


async def test_resolver_cleanup_removes_stale_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _audio_row()
    ciphertext, _mk, _fs, _fl = _encrypt_media(PLAINTEXT)
    monkeypatch.setattr(waha_media, "download_ciphertext", _fake_download(ciphertext))
    cache = tmp_path / "media"
    cache.mkdir()
    stale = cache / "audio-stale.ogg"
    stale.write_bytes(b"old")
    old = stale.stat().st_mtime - 48 * 3600
    import os

    os.utime(stale, (old, old))

    resolver = WahaMediaUrlResolver(FakeInbox(row), cache_dir=cache)
    await resolver.resolve(_domain_message("waha:gaddy-test:AUDIO-1"))
    assert not stale.exists()


def test_chained_resolver_dispatches_by_prefix() -> None:
    from echo_v2.services.waha_media import ChainedMediaUrlResolver

    @dataclass
    class FakeResolver:
        prefix: str

        async def resolve(self, message: object) -> str | None:
            return f"{self.prefix}-url"

    chain = ChainedMediaUrlResolver(
        {"baileys": FakeResolver("baileys"), "waha": FakeResolver("waha")}
    )
    import asyncio

    assert asyncio.run(chain.resolve(_domain_message("waha:s:m"))) == "waha-url"
    assert asyncio.run(chain.resolve(_domain_message("baileys:c:r"))) == "baileys-url"
    assert asyncio.run(chain.resolve(_domain_message("green:whatever"))) is None
