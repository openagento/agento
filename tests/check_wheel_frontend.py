"""A real wheel ships the built panel and the versioned kit (PRD E8 §11).

Not a pytest test: it needs the frontend build, so it runs after it (bin/test step 9, CI
test-js, the release build job). `python3 tests/check_wheel_frontend.py [wheel]` builds a wheel
when none is given.
"""
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[1]


def missing(names: set[str]) -> list[str]:
    kit = json.loads((ROOT / "frontend/packages/miniapp-kit/package.json").read_text())["version"]
    want = ["agento/framework/web/panel/index.html", "agento/modules/miniapps/skills/miniapp-ui/SKILL.md"]
    want += [f"agento/framework/web/miniapp-ui/{kit}/{f}" for f in
             ("agento-ui.css", "agento-ui.js", "agento-bridge.js", "fonts/inter-latin-400-normal.woff2")]
    out = [n for n in want if n not in names]
    if not any(n.startswith("agento/framework/web/panel/assets/") and n.endswith(".js") for n in names):
        out.append("agento/framework/web/panel/assets/*.js")
    return out


def check(wheel: Path) -> int:
    with zipfile.ZipFile(wheel) as z:
        gaps = missing(set(z.namelist()))
    print(f"{wheel.name}: " + ("missing " + ", ".join(gaps) if gaps else "panel and kit present"))
    return 1 if gaps else 0


def main(argv: list[str]) -> int:
    if argv:
        return check(Path(argv[0]))
    with tempfile.TemporaryDirectory() as out:
        subprocess.run(["uv", "build", "--wheel", "--no-sources", "-o", out], cwd=ROOT, check=True,
                       capture_output=True)
        (wheel,) = Path(out).glob("*.whl")
        return check(wheel)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
