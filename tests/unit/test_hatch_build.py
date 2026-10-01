"""The wheel hook refuses a release wheel built without the frontend (PRD E8 §11)."""
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("hatch_build", ROOT / "hatch_build.py")
hatch_build = importlib.util.module_from_spec(spec)
# hatchling exists only in the build environment; the hook's base class is all it needs.
_iface = types.ModuleType("hatchling.builders.hooks.plugin.interface")
_iface.BuildHookInterface = type("BuildHookInterface", (), {})
for name in ("hatchling", "hatchling.builders", "hatchling.builders.hooks", "hatchling.builders.hooks.plugin"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules.setdefault(_iface.__name__, _iface)
spec.loader.exec_module(hatch_build)


def test_missing_frontend_is_named(tmp_path):
    assert hatch_build.missing(tmp_path) == list(hatch_build.REQUIRED)


def _build(root, kit_files):
    web = root / "src/agento/framework/web"
    (web / "panel").mkdir(parents=True)
    (web / "panel/index.html").write_text("<!doctype html>")
    for f in kit_files:
        (web / "miniapp-ui/1.0.0" / f).parent.mkdir(parents=True, exist_ok=True)
        (web / "miniapp-ui/1.0.0" / f).write_text("x")


def test_built_frontend_passes(tmp_path):
    _build(tmp_path, ["agento-ui.css", "agento-ui.js", "agento-bridge.js", "fonts/inter-latin-400-normal.woff2"])
    assert hatch_build.missing(tmp_path) == []


def test_an_empty_kit_is_refused(tmp_path):
    _build(tmp_path, [])
    (tmp_path / "src/agento/framework/web/miniapp-ui/1.0.0").mkdir(parents=True)
    assert hatch_build.missing(tmp_path) == list(hatch_build.REQUIRED[1:])


def test_an_empty_file_does_not_count(tmp_path):
    _build(tmp_path, ["agento-ui.css", "agento-ui.js", "agento-bridge.js", "fonts/inter-latin-400-normal.woff2"])
    (tmp_path / "src/agento/framework/web/panel/index.html").write_text("")
    assert hatch_build.missing(tmp_path) == [hatch_build.REQUIRED[0]]
