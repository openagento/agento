"""AES-256-CBC encryption/decryption for core_config_data obscure fields.

Format: "aes256:{iv_hex}:{ciphertext_hex}"
Key: derived from AGENTO_ENCRYPTION_KEY env var via SHA-256.

Compatible with docker/toolbox/crypto.js (same algorithm, same key derivation).
"""
from __future__ import annotations

import hashlib
import os
from binascii import hexlify, unhexlify

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import store_env


def _derive_key() -> bytes:
    passphrase = store_env.get("AGENTO_ENCRYPTION_KEY", "")
    if not passphrase:
        raise RuntimeError("AGENTO_ENCRYPTION_KEY not set — cannot encrypt/decrypt")
    return hashlib.sha256(passphrase.encode()).digest()


def encrypt(plaintext: str) -> str:
    key = _derive_key()
    iv = os.urandom(16)

    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    return f"aes256:{hexlify(iv).decode()}:{hexlify(ciphertext).decode()}"


def decrypt(encoded: str) -> str:
    key = _derive_key()

    parts = encoded.split(":")
    if len(parts) != 3 or parts[0] != "aes256":
        raise ValueError('Invalid encrypted format: expected "aes256:{iv}:{ciphertext}"')

    iv = unhexlify(parts[1])
    ciphertext = unhexlify(parts[2])

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()

    return plaintext.decode()
