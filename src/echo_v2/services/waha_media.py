"""WhatsApp client-side E2E media decryption for WAHA messages (Phase 2).

The WAHA batch envelope carries the raw WhatsApp CDN URL plus the message's
``mediaKey`` — the CDN serves *ciphertext*, so unlike the Baileys connector
(which serves already-decrypted media URLs), Echo must decrypt itself.
Ported from the verified ``decrypt_media.py`` proof:

1. GET the URL from the envelope → ciphertext from the WhatsApp CDN
   (bounded streaming download; the trailing 10-byte truncated MAC is
   included but not verified — see step 4).
2. HKDF-SHA256(mediaKey, info="WhatsApp <Type> Keys", 112)
   → IV = keys[0:16], AES-256 key = keys[16:48].
3. AES-256-CBC decrypt (no unpadding), truncate to ``fileLength``.
4. Verify SHA-256(plaintext) == ``fileSHA256`` — the end-to-end integrity
   check, which supersedes the trailing-MAC scheme.

Decrypted bytes are handed to the enrichment pipeline via a ``file://``
temp path (content-addressed, age-cleaned) — binaries are never persisted,
matching the user decision for Phase 2.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from echo_v2.domain.waha import WahaInboxMessage, WahaMediaPayload
from echo_v2.services.transcription import suffix_from_mime
from echo_v2.services.waha_messages import extract_media

__all__ = [
    "ChainedMediaUrlResolver",
    "WahaMediaDecryptError",
    "WahaMediaDecryptor",
    "WahaMediaDownloadError",
    "WahaMediaError",
    "WahaMediaUnavailableError",
    "WahaMediaUrlResolver",
]

_logger = logging.getLogger("echo_v2.services.waha_media")

# HKDF info string per media type (WhatsApp protocol constant).
_MEDIA_INFO_STRINGS: dict[str, str] = {
    "audio": "WhatsApp Audio Keys",
    "image": "WhatsApp Image Keys",
    "video": "WhatsApp Video Keys",
    "document": "WhatsApp Document Keys",
    "sticker": "WhatsApp Sticker Keys",
}
_HKDF_LENGTH = 112
_IV_LENGTH = 16
_KEY_LENGTH = 32
_MAX_REDIRECTS = 3
_TEMP_MAX_AGE_SECONDS = 24 * 3600

_DEFAULT_CDN_USER_AGENT = "WhatsApp/2.0"


class WahaMediaError(RuntimeError):
    """Raised when WhatsApp media cannot be downloaded or decrypted.

    Subclasses classify the failure stage for the Operations Center media
    metrics (retrieval vs decryption vs unavailable reference) — the class
    name is recorded as ``error_type`` on enrichment failure events.
    """


class WahaMediaUnavailableError(WahaMediaError):
    """The media reference/envelope is missing or malformed."""


class WahaMediaDownloadError(WahaMediaError):
    """The ciphertext could not be retrieved from the CDN."""


class WahaMediaDecryptError(WahaMediaError):
    """Decryption or integrity verification failed."""


class WahaMediaDecryptor:
    """HKDF + AES-256-CBC decryption of WhatsApp CDN media."""

    def decrypt(
        self,
        *,
        ciphertext: bytes,
        media_key: str,
        media_type: str,
        file_length: int,
        file_sha256: str,
    ) -> bytes:
        """Decrypt and verify one media file.

        ``media_key`` and ``file_sha256`` are the base64 strings from the
        envelope. Raises :class:`WahaMediaError` on any protocol mismatch —
        the caller treats it as terminal (retrying won't help).
        """
        info = _MEDIA_INFO_STRINGS.get(media_type)
        if info is None:
            raise WahaMediaDecryptError(f"unsupported media type: {media_type}")
        try:
            key_bytes = base64.b64decode(media_key, validate=True)
            expected_sha = base64.b64decode(file_sha256, validate=True)
        except ValueError as exc:
            raise WahaMediaDecryptError(f"invalid base64 media fields: {exc}") from exc

        keys = self._hkdf_sha256(key_bytes, info, _HKDF_LENGTH)
        iv, cipher_key = keys[:_IV_LENGTH], keys[_IV_LENGTH : _IV_LENGTH + _KEY_LENGTH]
        if len(iv) != _IV_LENGTH or len(cipher_key) != _KEY_LENGTH:
            raise WahaMediaDecryptError("HKDF derived short key material")

        # The ciphertext ends with a 10-byte truncated HMAC; the plaintext
        # SHA-256 check below is the end-to-end integrity gate, so the MAC
        # bytes are simply not part of the AES input.
        aes_input = ciphertext[:-10] if len(ciphertext) > 10 else b""
        if len(aes_input) % _IV_LENGTH != 0:
            raise WahaMediaDecryptError(
                f"ciphertext length {len(aes_input)} is not block-aligned"
            )
        if len(aes_input) < file_length:
            raise WahaMediaDecryptError(
                f"ciphertext shorter than declared file length {file_length}"
            )
        decryptor = Cipher(
            algorithms.AES(cipher_key), modes.CBC(iv)
        ).decryptor()
        padded = decryptor.update(aes_input) + decryptor.finalize()
        plaintext = padded[:file_length]
        if hashlib.sha256(plaintext).digest() != expected_sha:
            raise WahaMediaDecryptError("plaintext SHA-256 mismatch")
        return plaintext

    @staticmethod
    def _hkdf_sha256(ikm: bytes, info: str, length: int) -> bytes:
        """RFC 5869 HKDF with SHA-256 (empty salt), matching WhatsApp.

        Identical to the verified ``decrypt_media.py`` proof; a unit test
        asserts equivalence with :mod:`cryptography`'s HKDF.
        """
        prk = hmac.new(b"\x00" * 32, ikm, hashlib.sha256).digest()
        okm = b""
        block = b""
        index = 1
        while len(okm) < length:
            block = hmac.new(
                prk, block + info.encode("utf-8") + bytes([index]), hashlib.sha256
            ).digest()
            okm += block
            index += 1
        return okm[:length]


async def download_ciphertext(
    url: str, *, max_bytes: int, user_agent: str = _DEFAULT_CDN_USER_AGENT
) -> bytes:
    """Stream the ciphertext from the WhatsApp CDN, aborting on overflow.

    HTTPS-only (the URL originates from an authenticated agent's envelope;
    scheme validation is defense in depth). Redirects are not followed —
    CDN URLs are direct.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise WahaMediaDownloadError("media URL must be an absolute HTTPS URL")
    buf = bytearray()
    async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
        response = await client.get(url, headers={"User-Agent": user_agent})
        if response.status_code in (301, 302, 303, 307, 308):
            raise WahaMediaDownloadError("media URL returned an unexpected redirect")
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise WahaMediaDownloadError(
                    f"media exceeded size cap {max_bytes} bytes "
                    f"(at least {len(buf)})"
                )
    return bytes(buf)


@runtime_checkable
class _WahaInboxReader(Protocol):
    async def get(self, session: str, message_id: str) -> WahaInboxMessage | None: ...


class WahaMediaUrlResolver:
    """Media URL resolver for WAHA messages (the Baileys resolver's analogue).

    Loads the inbox row on demand, downloads the ciphertext from the CDN,
    decrypts it, and hands the plaintext to the enrichment pipeline as a
    ``file://`` temp path (content-addressed by plaintext SHA-256). Binaries
    are never persisted — the temp cache is age-cleaned on each resolve.
    """

    def __init__(
        self,
        inbox: _WahaInboxReader,
        *,
        decryptor: WahaMediaDecryptor | None = None,
        max_bytes: int = 16 * 1024 * 1024,
        cache_dir: Path | None = None,
    ) -> None:
        self._inbox = inbox
        self._decryptor = decryptor or WahaMediaDecryptor()
        self._max_bytes = max_bytes
        self._cache_dir = cache_dir or Path(tempfile.gettempdir()) / "echo-waha-media"

    async def resolve(self, message: Any) -> str | None:
        """Return a ``file://`` path to the decrypted media, or ``None``.

        ``message`` is the domain :class:`~echo_v2.domain.chat.Message` whose
        ``media_reference`` is ``waha:<session>:<message_id>``.
        """
        reference = getattr(message, "media_reference", None)
        if not isinstance(reference, str) or not reference.startswith("waha:"):
            return None
        parts = reference.split(":", 2)
        if len(parts) != 3:
            raise WahaMediaUnavailableError(f"invalid WAHA media reference: {reference}")
        _, session, message_id = parts
        row = await self._inbox.get(session, message_id)
        if row is None:
            raise WahaMediaUnavailableError(
                f"inbox row not found for media reference {reference}"
            )
        media = extract_media(row)
        if media is None:
            _logger.info(
                "waha media resolve unavailable session=%s message_id=%s "
                "reason=no_media_in_envelope",
                session,
                message_id,
            )
            return None

        cache_path = self._cache_path_for(media)
        if not cache_path.exists():
            started = time.monotonic()
            ciphertext = await download_ciphertext(media.url, max_bytes=self._max_bytes)
            plaintext = self._decryptor.decrypt(
                ciphertext=ciphertext,
                media_key=media.media_key,
                media_type=media.media_type,
                file_length=media.file_length,
                file_sha256=media.file_sha256,
            )
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(plaintext)
            _logger.info(
                "waha media decrypted session=%s message_id=%s media_type=%s "
                "plaintext_bytes=%d duration_ms=%.1f",
                session,
                message_id,
                media.media_type,
                len(plaintext),
                (time.monotonic() - started) * 1000,
            )
        self._cleanup_stale()
        return cache_path.as_uri()

    def _cache_path_for(self, media: WahaMediaPayload) -> Path:
        digest = hashlib.sha256(media.file_sha256.encode("utf-8")).hexdigest()[:32]
        suffix = _suffix_for_mimetype(media.mimetype)
        return self._cache_dir / f"{media.media_type}-{digest}{suffix}"

    def _cleanup_stale(self) -> None:
        """Best-effort removal of temp files older than a day."""
        try:
            cutoff = time.time() - _TEMP_MAX_AGE_SECONDS
            for path in self._cache_dir.iterdir():
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
        except OSError:
            _logger.debug("waha media cache cleanup failed", exc_info=True)


def _suffix_for_mimetype(mimetype: str | None) -> str:
    return suffix_from_mime(mimetype or "")


class ChainedMediaUrlResolver:
    """Dispatches media resolution by ``media_reference`` prefix.

    Messages carry ``<provider>:<...>`` references (``baileys:`` → the
    connector resolver, ``waha:`` → the decrypting resolver). Unmatched
    references return ``None`` — the enrichment layer then marks the media
    unavailable instead of retrying.
    """

    def __init__(self, resolvers: dict[str, Any]) -> None:
        self._resolvers = resolvers

    async def resolve(self, message: Any) -> str | None:
        reference = getattr(message, "media_reference", None)
        if not isinstance(reference, str) or not reference:
            return None
        prefix = reference.split(":", 1)[0]
        resolver = self._resolvers.get(prefix)
        if resolver is None:
            return None
        resolved: str | None = await resolver.resolve(message)
        return resolved
