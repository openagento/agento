"""Tests for config_resolver — 3-level fallback resolution."""
import json
from dataclasses import dataclass, field
from pathlib import Path

from agento.framework.config_resolver import (
    read_config_defaults,
    resolve_field,
    resolve_module_config,
    resolve_module_config_with_sources,
    resolve_tool_field,
)


@dataclass
class FakeManifest:
    name: str = "testmod"
    config: dict = field(default_factory=dict)
    path: Path = field(default_factory=lambda: Path("/fake"))


# ---------------------------------------------------------------------------
# resolve_field: module-level config
# ---------------------------------------------------------------------------


class TestResolveFieldFallback:
    """Test that the 3-level fallback works in correct priority order."""

    SCHEMA = {"type": "string"}  # noqa: RUF012

    def test_env_wins_over_all(self, monkeypatch):
        monkeypatch.setenv("CONFIG__TESTMOD__HOST", "from-env")
        db = {"testmod/host": ("from-db", False)}
        defaults = {"host": "from-config"}

        result = resolve_field("testmod", "host", {"type": "string"}, defaults, db)
        assert result.value == "from-env"
        assert result.source == "env"

    def test_db_wins_over_config(self):
        db = {"testmod/host": ("from-db", False)}
        defaults = {"host": "from-config"}

        result = resolve_field("testmod", "host", {"type": "string"}, defaults, db)
        assert result.value == "from-db"
        assert result.source == "db"

    def test_config_json_fallback(self):
        result = resolve_field("testmod", "host", {"type": "string"}, {"host": "from-config"}, {})
        assert result.value == "from-config"
        assert result.source == "config.json"

    def test_schema_default_ignored(self):
        """Schema defaults are no longer used — only config.json defaults matter."""
        schema = {"type": "string", "default": "fallback"}
        result = resolve_field("testmod", "host", schema, {}, {})
        assert result.value is None
        assert result.source == "none"

    def test_none_when_no_source(self):
        result = resolve_field("testmod", "host", {"type": "string"}, {}, {})
        assert result.value is None
        assert result.source == "none"


class TestResolveFieldEnvNaming:
    """ENV key: CONFIG__{MODULE}__{FIELD} uppercase, hyphens -> underscores."""

    def test_hyphens_converted(self, monkeypatch):
        monkeypatch.setenv("CONFIG__MY_MOD__MY_FIELD", "val")
        result = resolve_field("my-mod", "my-field", {"type": "string"}, {}, {})
        assert result.value == "val"

    def test_case_insensitive_module(self, monkeypatch):
        monkeypatch.setenv("CONFIG__MYMOD__HOST", "val")
        result = resolve_field("mymod", "host", {"type": "string"}, {}, {})
        assert result.value == "val"


class TestResolveFieldDbPath:
    """DB path: {module}/{field}, hyphens -> underscores."""

    def test_simple_path(self):
        db = {"testmod/host": ("db-val", False)}
        result = resolve_field("testmod", "host", {"type": "string"}, {}, db)
        assert result.value == "db-val"

    def test_hyphens_converted(self):
        db = {"my_mod/my_field": ("db-val", False)}
        result = resolve_field("my-mod", "my-field", {"type": "string"}, {}, db)
        assert result.value == "db-val"


class TestResolveFieldEncrypted:
    """DB values with encrypted=True are decrypted."""

    def test_encrypted_db_value(self, monkeypatch):
        monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "test-key-1234")
        from agento.framework.crypto import encrypt

        encrypted_val = encrypt("secret123")
        db = {"testmod/pass": (encrypted_val, True)}

        result = resolve_field("testmod", "pass", {"type": "obscure"}, {}, db)
        assert result.value == "secret123"
        assert result.source == "db"

    def test_encrypted_without_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("AGENTO_ENCRYPTION_KEY", raising=False)
        db = {"testmod/pass": ("aes256:bad:data", True)}

        result = resolve_field("testmod", "pass", {"type": "obscure"}, {}, db)
        assert result.value is None
        assert result.source == "none"


class TestResolveFieldTypeCoercion:
    """Type coercion from string ENV/DB values."""

    def test_integer_from_env(self, monkeypatch):
        monkeypatch.setenv("CONFIG__TESTMOD__PORT", "3307")
        result = resolve_field("testmod", "port", {"type": "integer"}, {}, {})
        assert result.value == 3307

    def test_integer_from_db(self):
        db = {"testmod/port": ("8080", False)}
        result = resolve_field("testmod", "port", {"type": "integer"}, {}, db)
        assert result.value == 8080

    def test_boolean_true_variants(self, monkeypatch):
        for val in ("1", "true", "yes"):
            monkeypatch.setenv("CONFIG__TESTMOD__ENABLED", val)
            result = resolve_field("testmod", "enabled", {"type": "boolean"}, {}, {})
            assert result.value is True, f"Expected True for {val}"

    def test_boolean_false_variants(self, monkeypatch):
        for val in ("0", "false", "no"):
            monkeypatch.setenv("CONFIG__TESTMOD__ENABLED", val)
            result = resolve_field("testmod", "enabled", {"type": "boolean"}, {}, {})
            assert result.value is False, f"Expected False for {val}"

    def test_json_from_env(self, monkeypatch):
        monkeypatch.setenv("CONFIG__TESTMOD__MAP", '{"a": 1}')
        result = resolve_field("testmod", "map", {"type": "json"}, {}, {})
        assert result.value == {"a": 1}

    def test_config_json_integer_not_coerced(self):
        """config.json values are already parsed JSON types."""
        result = resolve_field("testmod", "port", {"type": "integer"}, {"port": 3306}, {})
        assert result.value == 3306

    def test_config_json_not_coerced(self):
        """config.json values are already parsed JSON types."""
        result = resolve_field("testmod", "port", {"type": "integer"}, {"port": 3307}, {})
        assert result.value == 3307


# ---------------------------------------------------------------------------
# resolve_tool_field
# ---------------------------------------------------------------------------


class TestResolveToolField:
    """Tool field resolution: CONFIG__{MOD}__TOOLS__{TOOL}__{FIELD}."""

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CONFIG__MYMOD__TOOLS__MYSQL_PROD__HOST", "env-host")
        result = resolve_tool_field(
            "mymod", "mysql_prod", "host", {"type": "string"}, {}, {}
        )
        assert result.value == "env-host"
        assert result.source == "env"

    def test_db_path_includes_tools(self):
        db = {"mymod/tools/mysql_prod/host": ("db-host", False)}
        result = resolve_tool_field(
            "mymod", "mysql_prod", "host", {"type": "string"}, {}, db
        )
        assert result.value == "db-host"
        assert result.source == "db"

    def test_config_json_nested_under_tools(self):
        defaults = {"tools": {"mysql_prod": {"host": "config-host"}}}
        result = resolve_tool_field(
            "mymod", "mysql_prod", "host", {"type": "string"}, defaults, {}
        )
        assert result.value == "config-host"
        assert result.source == "config.json"

    def test_schema_default_ignored(self):
        result = resolve_tool_field(
            "mymod", "mysql_prod", "port",
            {"type": "integer", "default": 3306}, {}, {}
        )
        assert result.value is None
        assert result.source == "none"


# ---------------------------------------------------------------------------
# resolve_module_config / resolve_module_config_with_sources
# ---------------------------------------------------------------------------


class TestResolveModuleConfig:
    def test_resolves_all_fields(self):
        manifest = FakeManifest(
            name="testmod",
            config={
                "host": {"type": "string"},
                "port": {"type": "integer"},
            },
        )
        defaults = {"host": "localhost", "port": 8080}
        result = resolve_module_config(manifest, defaults, {})
        assert result == {"host": "localhost", "port": 8080}

    def test_env_overrides_config_json(self, monkeypatch):
        monkeypatch.setenv("CONFIG__TESTMOD__HOST", "env-host")
        manifest = FakeManifest(
            name="testmod",
            config={"host": {"type": "string"}},
        )
        result = resolve_module_config(manifest, {"host": "localhost"}, {})
        assert result["host"] == "env-host"

    def test_with_sources(self):
        manifest = FakeManifest(
            name="testmod",
            config={
                "host": {"type": "string"},
                "port": {"type": "integer"},
            },
        )
        result = resolve_module_config_with_sources(manifest, {"host": "localhost"}, {})
        assert result["host"].value == "localhost"
        assert result["host"].source == "config.json"
        assert result["port"].value is None
        assert result["port"].source == "none"


# ---------------------------------------------------------------------------
# read_config_defaults
# ---------------------------------------------------------------------------


class TestReadConfigDefaults:
    def test_reads_config_json(self, tmp_path):
        config = {"host": "localhost", "port": 3306}
        (tmp_path / "config.json").write_text(json.dumps(config))
        result = read_config_defaults(tmp_path)
        assert result == config

    def test_missing_file(self, tmp_path):
        result = read_config_defaults(tmp_path)
        assert result == {}

    def test_invalid_json(self, tmp_path):
        (tmp_path / "config.json").write_text("not json")
        result = read_config_defaults(tmp_path)
        assert result == {}

    def test_caches_by_mtime(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"host": "a"}))

        from agento.framework import config_resolver
        reads = {"n": 0}
        real_read = config_resolver.Path.read_text

        def counting_read(self, *a, **k):
            if self.name == "config.json":
                reads["n"] += 1
            return real_read(self, *a, **k)

        monkeypatch.setattr(config_resolver.Path, "read_text", counting_read)

        assert read_config_defaults(tmp_path) == {"host": "a"}
        assert read_config_defaults(tmp_path) == {"host": "a"}
        assert reads["n"] == 1  # second call served from cache (same mtime)

    def test_reread_when_mtime_changes(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"host": "a"}))
        assert read_config_defaults(tmp_path) == {"host": "a"}
        # Rewrite with a newer mtime
        import os
        cfg.write_text(json.dumps({"host": "b"}))
        os.utime(cfg, (cfg.stat().st_atime + 10, cfg.stat().st_mtime + 10))
        assert read_config_defaults(tmp_path) == {"host": "b"}


def test_a_decrypt_failure_is_not_reported_as_unset():
    """The lenient default falls back to config.json — which is why a corrupt
    encrypted key looked like an absent one. strict=True separates them."""
    # This test file imports neither `pytest` nor `patch` today (its header is
    # `import json` + dataclass/Path + names from config_resolver), so import
    # them here rather than assuming a module-level import that does not exist.
    from unittest.mock import patch

    import pytest

    from agento.framework.config_resolver import DecryptError, ScopedConfigService

    svc = ScopedConfigService.__new__(ScopedConfigService)
    svc._overrides = {"m/secret": ("not-a-ciphertext", True)}
    svc._scope_overrides = {}
    svc._conn = None
    svc._scope, svc._scope_id = "default", 0

    with patch("agento.framework.config_resolver.get_encryptor") as enc:
        enc.return_value.decrypt.side_effect = ValueError("bad token")
        assert svc.get("m/secret") is None            # today's behaviour, kept
        with pytest.raises(DecryptError) as excinfo:
            svc.get("m/secret", strict=True)

    # The exception must name the path and nothing else.
    assert "m/secret" in str(excinfo.value)
    assert "not-a-ciphertext" not in str(excinfo.value)
