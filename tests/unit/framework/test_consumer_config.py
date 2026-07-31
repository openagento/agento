"""Tests for ConsumerConfig framework dataclass."""
import pytest

from agento.framework.consumer_config import ConsumerConfig


class TestConsumerConfigDefaults:
    def test_defaults(self):
        cfg = ConsumerConfig()
        assert cfg.max_workers == 10
        assert cfg.concurrency == 10  # backward-compat alias
        assert cfg.poll_interval == 5.0
        assert cfg.job_timeout_seconds == 1200
        assert cfg.disable_llm is False

    def test_frozen(self):
        cfg = ConsumerConfig()
        with pytest.raises(AttributeError):
            cfg.max_workers = 4


class TestConsumerConfigFromEnv:
    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("AGENTO_CONSUMER_MAX_WORKERS", "2")
        monkeypatch.setenv("AGENTO_CONSUMER_POLL_INTERVAL", "10.0")
        monkeypatch.setenv("AGENTO_JOB_TIMEOUT_SECONDS", "600")

        cfg = ConsumerConfig.from_env()
        assert cfg.concurrency == 2
        assert cfg.poll_interval == 10.0
        assert cfg.job_timeout_seconds == 600
        assert cfg.disable_llm is False

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("AGENTO_JOB_TIMEOUT_SECONDS", "300")
        monkeypatch.setenv("DISABLE_LLM", "true")

        cfg = ConsumerConfig.from_env()
        assert cfg.job_timeout_seconds == 300
        assert cfg.disable_llm is True

    def test_disable_llm_variants(self, monkeypatch):
        for val in ("1", "true", "yes", "True", "YES"):
            monkeypatch.setenv("DISABLE_LLM", val)
            cfg = ConsumerConfig.from_env()
            assert cfg.disable_llm is True, f"Expected True for DISABLE_LLM={val}"

        for val in ("0", "false", "no", ""):
            monkeypatch.setenv("DISABLE_LLM", val)
            cfg = ConsumerConfig.from_env()
            assert cfg.disable_llm is False, f"Expected False for DISABLE_LLM={val}"

    def test_defaults(self):
        cfg = ConsumerConfig.from_env()
        assert cfg.concurrency == 10
        assert cfg.poll_interval == 5.0
        assert cfg.job_timeout_seconds == 1200
