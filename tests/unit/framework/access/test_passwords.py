from __future__ import annotations

import pytest

from agento.framework.access.passwords import (
    MAX_PASSWORD,
    MIN_PASSWORD,
    check_password_policy,
    hash_password,
    verify_password,
)


def test_round_trip():
    stored = hash_password("correct horse battery")
    assert stored.startswith("scrypt$16384$8$1$")
    assert verify_password("correct horse battery", stored)


def test_wrong_password():
    assert not verify_password("wrong horse battery", hash_password("correct horse battery"))


def test_salt_differs_per_hash():
    assert hash_password("correct horse battery") != hash_password("correct horse battery")


@pytest.mark.parametrize("stored", [None, "", "plain", "scrypt$1$2$3$zz$zz", "bcrypt$x$y$z$aa$bb"])
def test_missing_or_malformed_hash_is_false(stored):
    assert not verify_password("correct horse battery", stored)


def test_non_ascii_password():
    stored = hash_password("zażółć gęślą jaźń")
    assert verify_password("zażółć gęślą jaźń", stored)
    assert not verify_password("zazolc gesla jazn", stored)


def test_length_bounds():
    check_password_policy("x" * MIN_PASSWORD)
    check_password_policy("x" * MAX_PASSWORD)
    for bad in ("x" * (MIN_PASSWORD - 1), "x" * (MAX_PASSWORD + 1)):
        with pytest.raises(ValueError):
            check_password_policy(bad)
