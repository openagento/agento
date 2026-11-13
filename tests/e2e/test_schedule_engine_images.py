"""The schedule engine in the built images (AG-67 plan §5.1): ``validate()`` runs the engine in a
child process (``python -P -m agento.framework.schedule.engine``) from the mounted package, so a
local test cannot prove that the ``cron`` and ``web`` images load it and have python-dateutil.

Skips only when docker or the service is absent. A running container that cannot import the
engine or dateutil fails the test: that image cannot save or tick a schedule.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from agento.framework.cli._project import compose_file_flags
from agento.framework.schedule.engine import VALIDATE_DEADLINE_S
from tests.e2e._stack import PROJECT_ROOT

_COMPOSE = ["docker", "compose", *compose_file_flags(PROJECT_ROOT)]
_E2E = os.environ.get("AGENTO_E2E") == "1"
SERVICES = ("cron", "web")


def _running() -> set[str]:
    if shutil.which("docker") is None:
        return set()
    res = subprocess.run([*_COMPOSE, "ps", "--status", "running", "--services"],
                         capture_output=True, text=True, timeout=30)
    return set(res.stdout.split()) if res.returncode == 0 else set()


_UP = _running() if _E2E else set()

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not _E2E, reason="set AGENTO_E2E=1 to run")]

# Prints one JSON line: the outcome of validate() and the seconds it took, inside the container.
_SCRIPT = r'''
import json, sys, time
from datetime import datetime
from agento.framework.schedule import engine
t = time.monotonic()
try:
    fires = engine.validate(sys.argv[1], datetime(2027, 1, 1, 9, 0), "UTC", min_interval_minutes=5,
                            now=datetime(2027, 1, 1, 8, 0))
    out = {"fires": len(fires)}
except engine.InvalidSchedule as exc:
    out = {"code": exc.code}
out["seconds"] = time.monotonic() - t
print(json.dumps(out))
'''


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("rrule, expect", [
    ("FREQ=DAILY", "fires"),
    ("FREQ=MINUTELY;INTERVAL=7;BYMONTH=2;BYMONTHDAY=30;BYHOUR=1,2,3", "too_complex"),
])
def test_the_image_validates_and_stops_at_the_deadline(service, rrule, expect):
    if service not in _UP:
        pytest.skip(f"the dev stack's {service} container is not running")
    res = subprocess.run([*_COMPOSE, "exec", "-T", service, "python", "-c", _SCRIPT, rrule],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    if expect == "fires":
        assert out.get("fires", 0) > 0, out
    else:
        assert out.get("code") == "too_complex", out
        assert out["seconds"] <= VALIDATE_DEADLINE_S + 1, out
