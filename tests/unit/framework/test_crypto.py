import hashlib
import os
from binascii import hexlify

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from agento.framework import crypto, store_env

KEY = "test-passphrase"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(store_env, "get", lambda name, default="": KEY if name == "AGENTO_ENCRYPTION_KEY" else default)


def _legacy_encrypt(plaintext: str) -> str:
    key = hashlib.sha256(KEY.encode()).digest()
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return f"aes256:{hexlify(iv).decode()}:{hexlify(enc.update(padded) + enc.finalize()).decode()}"


def test_roundtrip_uses_the_salted_format():
    encoded = crypto.encrypt("s3cret")
    assert encoded.startswith("aes256s:")
    assert len(encoded.split(":")) == 4
    assert crypto.decrypt(encoded) == "s3cret"


def test_the_same_plaintext_gets_a_fresh_salt_each_time():
    a, b = crypto.encrypt("same"), crypto.encrypt("same")
    assert a.split(":")[1] != b.split(":")[1]


def test_a_legacy_value_still_decrypts():
    legacy = _legacy_encrypt("old-token")
    assert crypto.is_legacy(legacy)
    assert crypto.decrypt(legacy) == "old-token"
    assert not crypto.is_legacy(crypto.encrypt("new-token"))


def test_a_malformed_value_is_refused():
    with pytest.raises(ValueError):
        crypto.decrypt("plaintext")
