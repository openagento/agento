"""AES-256-CBC encryption/decryption for core_config_data obscure fields.

Format: ``aes256s:{salt_hex}:{iv_hex}:{ciphertext_hex}`` — the key is derived from
AGENTO_ENCRYPTION_KEY with **scrypt** (per-value random salt), so a stolen database
cannot be attacked with a precomputed table and each guess costs real memory.

The legacy ``aes256:{iv_hex}:{ciphertext_hex}`` (bare SHA-256 of the passphrase) is
still *decrypted* so a deployment keeps working before the re-encryption data patch
(``core/RekeyToScrypt``) runs. Nothing writes that format any more.

**OBSOLETE — remove in 0.18.0.** The patch ships in 0.17.0, so by 0.18.0 every deployment
that upgraded has been rekeyed. Delete the legacy branch of :func:`decrypt`, :func:`is_legacy`
and the patch then.

Compatible with src/agento/toolbox/crypto.js (same algorithm, same key derivation).
"""
from __future__ import annotations

import hashlib
import os
from binascii import hexlify, unhexlify
from functools import lru_cache

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import store_env

# 16 MiB / ~100 ms per derivation. Mirrored in toolbox/crypto.js — changing either
# side alone makes every stored value undecryptable by the other container.
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16


def _passphrase() -> str:
    passphrase = store_env.get("AGENTO_ENCRYPTION_KEY", "")
    if not passphrase:
        raise RuntimeError("AGENTO_ENCRYPTION_KEY not set — cannot encrypt/decrypt")
    return passphrase


@lru_cache(maxsize=256)
def _scrypt_key(passphrase: str, salt: bytes) -> bytes:
    """Derive the AES key. Cached: bootstrap decrypts many values per process."""
    return hashlib.scrypt(
        passphrase.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )


def encrypt(plaintext: str) -> str:
    salt = os.urandom(SALT_BYTES)
    key = _scrypt_key(_passphrase(), salt)
    iv = os.urandom(16)

    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    return f"aes256s:{hexlify(salt).decode()}:{hexlify(iv).decode()}:{hexlify(ciphertext).decode()}"


def is_legacy(encoded: str) -> bool:
    """True for a value still stored under the pre-scrypt key derivation.

    OBSOLETE — remove in 0.18.0 together with the legacy branch of :func:`decrypt`.
    """
    return encoded.startswith("aes256:")


def decrypt(encoded: str) -> str:
    parts = encoded.split(":")
    if parts[0] == "aes256s" and len(parts) == 4:
        key = _scrypt_key(_passphrase(), unhexlify(parts[1]))
        iv, ciphertext = unhexlify(parts[2]), unhexlify(parts[3])
    elif parts[0] == "aes256" and len(parts) == 3:
        # OBSOLETE — remove in 0.18.0. Read-only path for values written before the
        # scrypt rekey; the data patch core/RekeyToScrypt rewrites them. Nothing
        # derives a new key this way.
        key = hashlib.sha256(_passphrase().encode()).digest()  # codeql[py/weak-sensitive-data-hashing]
        iv, ciphertext = unhexlify(parts[1]), unhexlify(parts[2])
    else:
        raise ValueError(
            'Invalid encrypted format: expected "aes256s:{salt}:{iv}:{ciphertext}"'
        )

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()

    return plaintext.decode()
