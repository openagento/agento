"""Round 9: the module set is selected once, and a gate never fails open.

Each test pins a defect SHAPE, not the one call site the review named.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import patch

import pytest

SRC = Path(__file__).resolve().parents[3] / "src" / "agento"


def _module(name: str, extra: dict | None = None) -> str:
    return json.dumps({"name": name, "version": "1.0.0", **(extra or {})})


class TestOneModuleSelection:
    """A consumer that derives its own root pair sees a different module set."""

    def test_no_source_file_scans_a_single_module_root(self):
        # `scan_modules` is the per-root primitive; only the two discovery functions
        # may call it. Anything else is a second, drifting module set.
        offenders = []
        for path in SRC.rglob("*.py"):
            if path.name in ("module_loader.py", "module_discovery.py"):
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "scan_modules":
                    offenders.append(f"{path}:{node.lineno}")
        assert offenders == []

    @pytest.fixture
    def collision(self, tmp_path):
        """The same module name in core and in app/code, with different content."""
        for root, version in (("core", "1.0.0"), ("user", "2.0.0")):
            d = tmp_path / root / "dup"
            d.mkdir(parents=True)
            (d / "module.json").write_text(_module("dup", {"version": version}))
        return str(tmp_path / "core"), str(tmp_path / "user")

    def test_every_consumer_picks_the_local_copy(self, collision):
        from agento.framework import core_config, module_discovery

        core, user = collision
        manifests = module_discovery.scan_all_modules(core, user)
        dirs = dict(module_discovery.module_dirs_by_name(core, user))

        # Parsed manifests: one entry, the local one — not both copies.
        assert [m.version for m in manifests] == ["2.0.0"]
        assert dirs["dup"] == Path(user) / "dup"

        with patch("agento.framework.bootstrap.CORE_MODULES_DIR", core), \
             patch("agento.framework.bootstrap.USER_MODULES_DIR", user):
            assert core_config._find_module_dir("dup") == Path(user) / "dup"


class TestGatesFailClosed:
    def test_a_failed_module_scan_never_reports_a_clean_tool_state(self):
        """One malformed extension must not erase every declared `requires` relation."""
        from agento.framework import tool_enablement

        def boom(*_a, **_k):
            raise ValueError("malformed module.json")

        with patch("agento.framework.module_discovery.scan_all_modules", boom), \
             pytest.raises(ValueError):
            tool_enablement.scan_tool_requires()


class TestDeclarationsAreValidatedWithoutDiJson:
    def _validate(self, module_dir):
        from agento.framework.module_validator import validate_module

        return validate_module(module_dir)

    def test_a_malformed_declaration_in_module_json_is_rejected(self, tmp_path):
        """The loader falls back to module.json `provides`; the gate must follow it."""
        d = tmp_path / "legacy"
        d.mkdir()
        (d / "module.json").write_text(_module("legacy", {"provides": {"job_types": "chat"}}))
        assert any("job_types" in e for e in self._validate(d))

    def test_an_empty_di_json_still_falls_back_like_the_loader(self, tmp_path):
        d = tmp_path / "empty_di"
        d.mkdir()
        (d / "module.json").write_text(_module("empty_di", {"provides": {"job_types": "chat"}}))
        (d / "di.json").write_text("{}")
        assert any("job_types" in e for e in self._validate(d))

    def test_a_non_object_di_json_root_is_an_error(self, tmp_path):
        d = tmp_path / "list_di"
        d.mkdir()
        (d / "module.json").write_text(_module("list_di"))
        (d / "di.json").write_text("[]")
        assert any("must be an object" in e for e in self._validate(d))
