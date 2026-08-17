"""Credential encryption boundary for provider credentials at rest.

The ``credentials`` BYTEA column in ``whatsapp_connections`` stores
*encrypted* bytes, not raw :class:`ProviderCredentials.data`. ``repr=False``
on :class:`ProviderCredentials` protects logs; it does not protect a
database dump or a leaked backup. This module defines the cipher boundary
so the schema means "encrypted blob" from day one.

The cipher is constructed at app composition time from ``ECHO_CREDENTIAL_KEY``
(env). Repositories call ``encrypt`` on save and ``decrypt`` on retrieve;
callers never see the ciphertext. Swapping :class:`LocalKeyCredentialCipher`
for a KMS/secret-manager-backed implementation later changes one
composition-time wire, not the schema, the repos, or the protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cryptography.fernet import Fernet

__all__ = [
    "CredentialCipher",
    "IdentityCredentialCipher",
    "LocalKeyCredentialCipher",
]


@runtime_checkable
class CredentialCipher(Protocol):
    """Encrypt/decrypt provider credential bytes at the persistence boundary."""

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt ``plaintext``; returns opaque ciphertext bytes."""
        ...

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Decrypt ``ciphertext``; returns the original plaintext bytes.

        Raises if the ciphertext is invalid or the key does not match.
        """
        ...


class LocalKeyCredentialCipher:
    """Symmetric encryption keyed from ``ECHO_CREDENTIAL_KEY``.

    Uses :class:`cryptography.fernet.Fernet` (AES-128-CBC + HMAC-SHA256).
    Suitable for local dev and single-instance deploys. Replace with a
    KMS/secret-manager-backed :class:`CredentialCipher` in production
    without changing repositories.

    The key is a 44-char url-safe base64 string (``Fernet.generate_key()``).
    """

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return self._fernet.decrypt(ciphertext)


class IdentityCredentialCipher:
    """No-op cipher for tests.

    Stores plaintext as-is so persistence tests don't depend on key config.
    Never use in production — the whole point of the boundary is that the
    BYTEA column holds encrypted bytes.
    """

    def encrypt(self, plaintext: bytes) -> bytes:
        return plaintext

    def decrypt(self, ciphertext: bytes) -> bytes:
        return ciphertext


# Structural check: both concrete ciphers satisfy the protocol.
_: CredentialCipher = LocalKeyCredentialCipher(Fernet.generate_key())  # type: ignore[assignment]
__: CredentialCipher = IdentityCredentialCipher()  # type: ignore[assignment]
