"""Tests for Encryptor protocol, get/set_encryptor, and AesCbcBackend."""
from __future__ import annotations

import pytest

from agento.framework.encryptor import _FallbackEncryptor, get_encryptor, set_encryptor


class TestGetEncryptor:
    def test_returns_fallback_when_none_registered(self):
        # Reset global state
        import agento.framework.encryptor as mod
        old = mod._instance
        mod._instance = None
        try:
            enc = get_encryptor()
            assert isinstance(enc, _FallbackEncryptor)
        finally:
            mod._instance = old

    def test_returns_registered_backend(self):
        import agento.framework.encryptor as mod
        old = mod._instance

        class DummyBackend:
            def encrypt(self, plaintext: str) -> str:
                return f"dummy:{plaintext}"
            def decrypt(self, ciphertext: str) -> str:
                return ciphertext.replace("dummy:", "")

        try:
            set_encryptor(DummyBackend())
            enc = get_encryptor()
            assert enc.encrypt("hello") == "dummy:hello"
            assert enc.decrypt("dummy:hello") == "hello"
        finally:
            mod._instance = old


class TestFallbackEncryptor:
    def test_roundtrip(self, monkeypatch):
        monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "test-key-for-fallback")
        enc = _FallbackEncryptor()
        encrypted = enc.encrypt("secret-value")
        assert encrypted.startswith("aes256s:")
        assert enc.decrypt(encrypted) == "secret-value"

    def test_raises_without_key(self, monkeypatch):
        monkeypatch.delenv("AGENTO_ENCRYPTION_KEY", raising=False)
        enc = _FallbackEncryptor()
        with pytest.raises(RuntimeError, match="AGENTO_ENCRYPTION_KEY"):
            enc.encrypt("value")


class TestAesCbcBackend:
    def test_roundtrip(self, monkeypatch):
        monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "test-key-for-backend")
        from agento.modules.crypt.src.aes_cbc_backend import AesCbcBackend
        backend = AesCbcBackend()
        encrypted = backend.encrypt("my-secret")
        assert encrypted.startswith("aes256s:")
        assert backend.decrypt(encrypted) == "my-secret"

    def test_conforms_to_protocol(self):
        from agento.modules.crypt.src.aes_cbc_backend import AesCbcBackend
        # Protocol structural check — AesCbcBackend has encrypt/decrypt methods
        assert hasattr(AesCbcBackend, "encrypt")
        assert hasattr(AesCbcBackend, "decrypt")


class TestRegisterEncryptorObserver:
    def test_registers_on_crypt_module(self, monkeypatch):
        monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "test-key")
        import agento.framework.encryptor as mod
        old = mod._instance
        mod._instance = None
        try:
            from agento.modules.crypt.src.aes_cbc_backend import AesCbcBackend
            from agento.modules.crypt.src.observers import RegisterEncryptorObserver

            class FakeEvent:
                name = "crypt"
                path = "/modules/crypt"

            observer = RegisterEncryptorObserver()
            observer.execute(FakeEvent())

            enc = get_encryptor()
            assert isinstance(enc, AesCbcBackend)
        finally:
            mod._instance = old

    def test_ignores_other_modules(self):
        import agento.framework.encryptor as mod
        old = mod._instance
        mod._instance = None
        try:
            from agento.modules.crypt.src.observers import RegisterEncryptorObserver

            class FakeEvent:
                name = "jira"
                path = "/modules/jira"

            observer = RegisterEncryptorObserver()
            observer.execute(FakeEvent())

            # Should still be None (fallback)
            assert mod._instance is None
        finally:
            mod._instance = old
