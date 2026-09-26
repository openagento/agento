"""Data patch: re-encrypt pre-scrypt values under the salted key derivation."""
from __future__ import annotations

import hashlib
import importlib.util
import os
from binascii import hexlify
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from agento.framework import crypto, store_env

PATCH_PATH = (
    Path(__file__).resolve().parents[4]
    / "src/agento/modules/core/src/patches/rekey_to_scrypt.py"
)
KEY = "rekey-passphrase"


def _load():
    spec = importlib.util.spec_from_file_location("rekey_to_scrypt", PATCH_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(
        store_env, "get",
        lambda name, default="": KEY if name == "AGENTO_ENCRYPTION_KEY" else default,
    )


def _legacy(plaintext: str) -> str:
    key = hashlib.sha256(KEY.encode()).digest()
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    body = hexlify(enc.update(padded) + enc.finalize()).decode()
    return f"aes256:{hexlify(iv).decode()}:{body}"


class FakeCursor:
    """Two tables keyed by the column the patch selects."""

    def __init__(self, config_rows, credential_rows):
        self.tables = {"value": config_rows, "credentials": credential_rows}
        self._column = None

    def execute(self, sql, params=()):
        self._column = "value" if "core_config_data" in sql else "credentials"
        self._id_column = "config_id" if self._column == "value" else "id"
        if sql.strip().upper().startswith("UPDATE"):
            new_value, row_id = params
            self.tables[self._column][row_id] = new_value
        else:
            assert params == ("aes256:%",)

    def fetchall(self):
        col = self._column
        return [
            {self._id_column: rid, col: value}
            for rid, value in self.tables[col].items()
            if value and value.startswith("aes256:")
        ]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


def test_legacy_rows_are_rewritten_and_still_decrypt_to_the_same_plaintext():
    fresh = crypto.encrypt("already-migrated")
    cursor = FakeCursor(
        {1: _legacy("db-password"), 2: fresh},
        {7: _legacy('{"token": "abc"}')},
    )
    conn = FakeConn(cursor)

    _load().RekeyToScrypt().apply(conn)

    assert conn.committed
    assert crypto.decrypt(cursor.tables["value"][1]) == "db-password"
    assert cursor.tables["value"][1].startswith("aes256s:")
    assert cursor.tables["value"][2] == fresh  # untouched
    assert crypto.decrypt(cursor.tables["credentials"][7]) == '{"token": "abc"}'


def test_rerunning_the_patch_changes_nothing():
    cursor = FakeCursor({1: _legacy("db-password")}, {})
    patch = _load().RekeyToScrypt()
    patch.apply(FakeConn(cursor))
    migrated = cursor.tables["value"][1]
    patch.apply(FakeConn(cursor))
    assert cursor.tables["value"][1] == migrated
