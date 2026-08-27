"""SSH key inspection for stored identities — parse, derive, fingerprint, pair-check.

Exists because a stored ``agent_view/identity/ssh_private_key`` was accepted,
reported as "stored (fingerprint sha256-tag:…)", and materialized as a 36-byte
``id_rsa`` containing nothing but the BEGIN header. A SHA-256 of the raw text is
deterministic for arbitrary garbage, so it validates nothing. These helpers do
the real thing: parse the key, derive its public half, and compare.

Uses ``cryptography`` (already a dependency) rather than shelling out to
``ssh-keygen``, so no private key is ever written to disk or passed on a
command line. Nothing here returns, logs, or raises private-key material.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

CODE_OK = "OK"
CODE_INVALID_KEY = "INVALID_KEY"
CODE_PAIR_MISMATCH = "PAIR_MISMATCH"
CODE_NOT_SET = "NOT_SET"
CODE_ENCRYPTED = "ENCRYPTED_KEY"
CODE_NO_PUBLIC = "NO_PUBLIC_KEY"


class EncryptedKeyError(ValueError):
    """The key parses but is passphrase-protected, so it cannot be derived here."""


@dataclass(frozen=True)
class KeyCheck:
    """Result of ``check_keypair``. ``detail`` is safe to print."""

    code: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.code == CODE_OK


def derive_public_key(private_text: str) -> str:
    """Return the OpenSSH public line (``"<type> <base64>"``, no comment).

    Raises ``EncryptedKeyError`` for a passphrase-protected key and ``ValueError``
    for anything unparsable. The exception message never quotes the input.
    """
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_pem_private_key,
        load_ssh_private_key,
    )

    text = (private_text or "").strip()
    if not text:
        raise ValueError("empty private key")
    data = text.encode() + b"\n"

    # Two formats are in the wild and cryptography needs a different loader for
    # each: `load_ssh_private_key` handles `-----BEGIN OPENSSH PRIVATE KEY-----`
    # and REJECTS a traditional PEM key ("Not OpenSSH private key format"), while
    # `load_pem_private_key` handles PEM (RSA/EC/PKCS8) and rejects OpenSSH. Try
    # both before concluding a key is invalid, or `ssh-keygen -m PEM` output —
    # a perfectly usable deployment key — is reported as garbage.
    key = None
    encrypted = False
    for loader in (load_ssh_private_key, load_pem_private_key):
        try:
            key = loader(data, password=None)
            break
        except TypeError:
            # Both loaders signal "a password is required" with TypeError.
            encrypted = True
        except Exception:
            continue
    if key is None:
        if encrypted:
            raise EncryptedKeyError("private key is passphrase-protected")
        raise ValueError("unparsable private key")
    return key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()


def openssh_fingerprint(public_line: str) -> str:
    """``"SHA256:<base64-no-padding>"`` — byte-identical to ``ssh-keygen -lf``.

    The comment field is ignored, so the same key fingerprints the same however
    it is labelled.
    """
    parts = (public_line or "").split()
    if len(parts) < 2:
        raise ValueError("not an OpenSSH public key line")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except Exception:
        raise ValueError("public key base64 is not decodable") from None
    if not blob:
        raise ValueError("public key blob is empty")
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _identity(public_line: str) -> tuple[str, str] | None:
    """``(type, base64)`` — the parts that define the key, comment excluded."""
    parts = (public_line or "").split()
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def check_keypair(private_text: str | None, public_text: str | None) -> KeyCheck:
    """Does the stored private key parse, and does it match the stored public key?

    Never raises, never echoes key material. Codes:
    ``NOT_SET`` (no private key), ``INVALID_KEY`` (does not parse),
    ``ENCRYPTED_KEY`` (passphrase-protected), ``NO_PUBLIC_KEY`` (private key is
    fine but there is nothing to compare it against), ``PAIR_MISMATCH``, ``OK``.
    """
    if not (private_text or "").strip():
        return KeyCheck(CODE_NOT_SET, "no private key stored")

    try:
        derived = derive_public_key(private_text)
    except EncryptedKeyError:
        return KeyCheck(
            CODE_ENCRYPTED,
            "private key is passphrase-protected — the agent cannot use it "
            "unattended; store an unencrypted key",
        )
    except ValueError as e:
        return KeyCheck(
            CODE_INVALID_KEY,
            f"stored private key does not parse: {e}. A truncated paste is the "
            f"usual cause — re-set it from a file: "
            f"agento config:set <path> --agent-view <code> < id_rsa",
        )

    fingerprint = openssh_fingerprint(derived)

    if not (public_text or "").strip():
        return KeyCheck(
            CODE_NO_PUBLIC,
            f"private key is valid ({fingerprint}) but no public key is stored, "
            f"so the pair cannot be confirmed",
        )

    stored = _identity(public_text)
    if stored is None:
        return KeyCheck(
            CODE_PAIR_MISMATCH,
            f"stored public key is not a valid OpenSSH public line; private key "
            f"is valid ({fingerprint})",
        )
    if stored != _identity(derived):
        return KeyCheck(
            CODE_PAIR_MISMATCH,
            f"stored public key does not match the private key "
            f"(private key is {fingerprint})",
        )
    return KeyCheck(CODE_OK, f"private and public key form a pair ({fingerprint})")


__all__ = [
    "CODE_ENCRYPTED", "CODE_INVALID_KEY", "CODE_NOT_SET", "CODE_NO_PUBLIC",
    "CODE_OK", "CODE_PAIR_MISMATCH",
    "EncryptedKeyError", "KeyCheck",
    "check_keypair", "derive_public_key",
    "openssh_fingerprint",
]
