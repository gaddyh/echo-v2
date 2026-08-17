"""Unit tests for the credential cipher boundary (no Docker needed)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from echo_v2.persistence.credential_cipher import (
    CredentialCipher,
    IdentityCredentialCipher,
    LocalKeyCredentialCipher,
)


def test_local_cipher_satisfies_protocol():
    cipher = LocalKeyCredentialCipher(Fernet.generate_key())
    assert isinstance(cipher, CredentialCipher)


def test_identity_cipher_satisfies_protocol():
    assert isinstance(IdentityCredentialCipher(), CredentialCipher)


def test_local_cipher_round_trips():
    key = Fernet.generate_key()
    cipher = LocalKeyCredentialCipher(key)
    plaintext = b"green-api-instance-111:token-secret"
    ciphertext = cipher.encrypt(plaintext)
    assert ciphertext != plaintext
    assert cipher.decrypt(ciphertext) == plaintext


def test_local_cipher_ciphertext_differs_for_same_plaintext():
    """Fernet includes a random IV — encrypting twice must not produce identical ciphertext."""
    key = Fernet.generate_key()
    cipher = LocalKeyCredentialCipher(key)
    a = cipher.encrypt(b"same-secret")
    b = cipher.encrypt(b"same-secret")
    assert a != b
    assert cipher.decrypt(a) == cipher.decrypt(b) == b"same-secret"


def test_local_cipher_wrong_key_fails_decrypt():
    cipher_a = LocalKeyCredentialCipher(Fernet.generate_key())
    cipher_b = LocalKeyCredentialCipher(Fernet.generate_key())
    ciphertext = cipher_a.encrypt(b"secret")
    with pytest.raises(InvalidToken):
        cipher_b.decrypt(ciphertext)


def test_local_cipher_tampered_ciphertext_fails():
    cipher = LocalKeyCredentialCipher(Fernet.generate_key())
    ciphertext = bytearray(cipher.encrypt(b"secret"))
    ciphertext[5] ^= 0xFF  # flip a bit
    with pytest.raises(InvalidToken):
        cipher.decrypt(bytes(ciphertext))


def test_identity_cipher_is_noop():
    cipher = IdentityCredentialCipher()
    assert cipher.encrypt(b"plaintext") == b"plaintext"
    assert cipher.decrypt(b"ciphertext") == b"ciphertext"


def test_local_cipher_empty_input_round_trips():
    cipher = LocalKeyCredentialCipher(Fernet.generate_key())
    assert cipher.decrypt(cipher.encrypt(b"")) == b""
