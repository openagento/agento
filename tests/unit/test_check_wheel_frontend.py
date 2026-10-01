"""The post-build wheel check refuses a wheel without the panel or the kit."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
_spec = importlib.util.spec_from_file_location("check_wheel_frontend", ROOT / "tests/check_wheel_frontend.py")
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)
KIT = json.loads((ROOT / "frontend/packages/miniapp-kit/package.json").read_text())["version"]
FULL = {"agento/framework/web/panel/index.html", "agento/framework/web/panel/assets/index-x.js",
        "agento/modules/miniapps/skills/miniapp-ui/SKILL.md",
        *(f"agento/framework/web/miniapp-ui/{KIT}/{f}" for f in
          ("agento-ui.css", "agento-ui.js", "agento-bridge.js", "fonts/inter-latin-400-normal.woff2"))}


def test_a_complete_wheel_passes():
    assert check.missing(FULL) == []


def test_a_wheel_without_the_kit_or_assets_fails():
    names = {n for n in FULL if "miniapp-ui" not in n and "assets" not in n}
    gaps = check.missing(names)
    assert f"agento/framework/web/miniapp-ui/{KIT}/agento-ui.css" in gaps
    assert "agento/framework/web/panel/assets/*.js" in gaps
