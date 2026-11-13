"""setup:upgrade pre-flight checks (AG-67 plan §6.3): a module's di.json `upgrade_checks`
run after the manifests are validated and before any DB change."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.module_validator import validate_module
from agento.framework.setup import UpgradeCheckError, setup_upgrade


@pytest.fixture(autouse=True)
def _ignore_host_module_status():
    with patch("agento.framework.module_status.read_module_status", return_value={}):
        yield


def _module(core: Path, name: str, check_body: str, *, section: str = "upgrade_checks") -> Path:
    mod = core / name
    (mod / "src").mkdir(parents=True)
    (mod / "module.json").write_text(json.dumps({"name": name, "version": "1.0.0", "description": name}))
    (mod / "di.json").write_text(json.dumps({section: [{"name": "c", "class": "src.check.Check"}]}))
    (mod / "src" / "check.py").write_text(
        "class Check:\n    def check(self, conn):\n" + "".join(f"        {ln}\n" for ln in check_body.splitlines()))
    return mod


def _run(tmp_path: Path, **kw):
    conn = MagicMock()
    return setup_upgrade(conn, logging.getLogger("test"), core_dir=str(tmp_path / "core"),
                         user_dir=str(tmp_path / "user"), skip_onboarding=True, **kw), conn


@pytest.fixture
def core(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "user").mkdir()
    return tmp_path / "core"


@pytest.mark.parametrize("dry_run", [False, True])
@patch("agento.framework.setup.get_pending", return_value=[])
@patch("agento.framework.setup.migrate", return_value=[])
def test_a_refusal_aborts_before_any_migration(migrate, _pending, core, tmp_path, dry_run):
    _module(core, "acme", "return ['frequency_map entry Hourly: unconvertible']")
    with pytest.raises(UpgradeCheckError, match="acme: frequency_map entry Hourly"):
        _run(tmp_path, dry_run=dry_run)
    migrate.assert_not_called()


@patch("agento.framework.setup.migrate", return_value=[])
def test_no_message_lets_the_upgrade_run(migrate, core, tmp_path):
    _module(core, "acme", "conn.probe()\nreturn []")
    _, conn = _run(tmp_path)
    conn.probe.assert_called_once()
    migrate.assert_called()


@patch("agento.framework.setup.migrate", return_value=[])
def test_a_check_that_raises_aborts(migrate, core, tmp_path):
    _module(core, "acme", "raise RuntimeError('boom')")
    with pytest.raises(RuntimeError, match="boom"):
        _run(tmp_path)
    migrate.assert_not_called()


@patch("agento.framework.setup.migrate", return_value=[])
def test_a_disabled_module_is_not_checked(migrate, core, tmp_path):
    _module(core, "acme", "return ['refused']")
    with patch("agento.framework.module_status.read_module_status", return_value={"acme": False}):
        _run(tmp_path)
    migrate.assert_called()


@pytest.mark.parametrize("section", ["upgrade_checks", "schedule_sources"])
def test_validator_checks_the_class_path(core, section):
    mod = _module(core, "acme", "return []", section=section)
    assert validate_module(mod) == []
    (mod / "src" / "check.py").unlink()
    assert any(section in e for e in validate_module(mod))
