"""The shipped ``frequency_map`` default is RRULE (AG-67 plan §6.1): each value is the converted
twin of the cron expression it replaced, so an upgraded deployment fires at the same minutes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.framework.schedule.cron_convert import cron_to_rrule

CONFIG = Path(__file__).parents[4] / "src/agento/modules/jira_periodic_tasks/config.json"
OLD_DEFAULTS = {
    "Every 5min": "*/5 * * * *",
    "Every 30min": "*/30 * * * *",
    "Every 1h": "0 * * * *",
    "Every 4h": "0 */4 * * *",
    "Daily at 8:00": "0 8 * * *",
    "Daily at 1:00": "0 1 * * *",
    "Twice daily at 6:00 and 18:00": "0 6,18 * * *",
    "Weekly (Mon, 7:00)": "0 7 * * 1",
}


def test_the_labels_are_unchanged():
    assert list(json.loads(CONFIG.read_text())["frequency_map"]) == list(OLD_DEFAULTS)


@pytest.mark.parametrize("label, cron", OLD_DEFAULTS.items())
def test_each_default_is_the_converted_cron(label, cron):
    assert json.loads(CONFIG.read_text())["frequency_map"][label] == cron_to_rrule(cron)
