"""Password hashing: stdlib scrypt, the work factors of versioned_artifacts/toolbox/auth.js."""
from __future__ import annotations

import hashlib
import hmac
import secrets

MIN_PASSWORD, MAX_PASSWORD = 12, 1024
_N, _R, _P, _DKLEN = 16384, 8, 1, 32
_MAXMEM = 64 * 1024 * 1024
# Spent on a missing or malformed hash so that an unknown user costs one derive too.
_DUMMY_SALT = bytes(16)


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_DKLEN, maxmem=_MAXMEM)


def check_password_policy(password: str) -> None:
    if not isinstance(password, str) or not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise ValueError(f"password must be {MIN_PASSWORD}-{MAX_PASSWORD} characters")


def hash_password(password: str) -> str:
    check_password_policy(password)
    salt = secrets.token_bytes(16)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${_derive(password, salt, _N, _R, _P).hex()}"


def dummy_verify(password: str) -> None:
    _derive(password[:MAX_PASSWORD], _DUMMY_SALT, _N, _R, _P)


def verify_password(password: str, stored: str | None) -> bool:
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        dummy_verify(password)  # an out-of-policy password costs one derive too, and never matches
        return False
    try:
        algo, n, r, p, salt, digest = (stored or "").split("$")
        if algo != "scrypt" or (int(n), int(r), int(p)) != (_N, _R, _P):
            raise ValueError
        salt_b, digest_b = bytes.fromhex(salt), bytes.fromhex(digest)
    except ValueError:
        dummy_verify(password)
        return False
    return hmac.compare_digest(_derive(password, salt_b, _N, _R, _P), digest_b)
