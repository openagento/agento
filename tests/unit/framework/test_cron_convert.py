"""cron -> RRULE (AG-67 plan §5.2): each converted rule fires at the same minutes as the cron."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agento.framework.schedule.cron_convert import Unconvertible, cron_to_rrule
from agento.framework.schedule.engine import next_fires

# The 8 default jira_periodic_tasks frequency_map entries (plan §6.1).
DEFAULT_MAP = [
    ("*/5 * * * *", "FREQ=HOURLY;BYMINUTE=0,5,10,15,20,25,30,35,40,45,50,55"),
    ("*/30 * * * *", "FREQ=HOURLY;BYMINUTE=0,30"),
    ("0 * * * *", "FREQ=HOURLY;BYMINUTE=0"),
    ("0 */4 * * *", "FREQ=DAILY;BYHOUR=0,4,8,12,16,20;BYMINUTE=0"),
    ("0 8 * * *", "FREQ=DAILY;BYHOUR=8;BYMINUTE=0"),
    ("0 1 * * *", "FREQ=DAILY;BYHOUR=1;BYMINUTE=0"),
    ("0 6,18 * * *", "FREQ=DAILY;BYHOUR=6,18;BYMINUTE=0"),
    ("0 7 * * 1", "FREQ=WEEKLY;BYDAY=MO;BYHOUR=7;BYMINUTE=0"),
]


@pytest.mark.parametrize("cron, rrule", DEFAULT_MAP)
def test_default_map_entries(cron, rrule):
    assert cron_to_rrule(cron) == rrule


@pytest.mark.parametrize("cron, rrule", [
    ("* * * * *", "FREQ=MINUTELY"),
    ("15 10 1,15 * *", "FREQ=MONTHLY;BYMONTHDAY=1,15;BYHOUR=10;BYMINUTE=15"),
    ("0 0 * 3 *", "FREQ=DAILY;BYMONTH=3;BYHOUR=0;BYMINUTE=0"),
    ("0 9 * * 0,7", "FREQ=WEEKLY;BYDAY=SU;BYHOUR=9;BYMINUTE=0"),
    ("0 9 * * 1-5", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0"),
    ("30 * * * 6", "FREQ=WEEKLY;BYDAY=SA;BYHOUR=" + ",".join(map(str, range(24))) + ";BYMINUTE=30"),
    ("0 8-18/5 * * *", "FREQ=DAILY;BYHOUR=8,13,18;BYMINUTE=0"),
])
def test_shapes(cron, rrule):
    assert cron_to_rrule(cron) == rrule


def _cron_minutes(cron: str, start: datetime, end: datetime) -> list[datetime]:
    """A brute-force cron matcher, the oracle for the equivalence check."""
    from agento.framework.schedule.cron_convert import _FIELDS, _expand
    sets = [set(_expand(f, lo, hi)) for f, (lo, hi) in zip(cron.split(), _FIELDS, strict=True)]
    sets[4] = {d % 7 for d in sets[4]}
    out, t = [], start
    while t < end:
        if (t.minute in sets[0] and t.hour in sets[1] and t.day in sets[2] and t.month in sets[3]
                and (t.isoweekday() % 7) in sets[4]):
            out.append(t)
        t += timedelta(minutes=1)
    return out


@pytest.mark.parametrize("cron", [c for c, _ in DEFAULT_MAP] + ["15 10 1,15 * *", "0 0 * 3 *", "0 9 * * 1-5"])
def test_fires_at_the_same_minutes_as_cron(cron):
    start, end = datetime(2026, 2, 26), datetime(2026, 3, 5)
    expected = _cron_minutes(cron, start, end)
    got = [f.local for f in next_fires(cron_to_rrule(cron), start, "UTC",
                                       after=start - timedelta(minutes=1), n=10_000) if f.local < end]
    assert got == expected


@pytest.mark.parametrize("cron", [
    "0 8 1 * 1",       # day-of-month AND day-of-week (cron ORs them)
    "0 8 L * *", "0 8 * * 1#2", "0 8 15W * *", "0 8 ? * *",
    "@daily", "0 8 * *", "0 8 * * * *", "60 8 * * *", "0 24 * * *", "*/0 * * * *",
    "0 8 * JAN *", "a b c d e", "5-1 * * * *", "",
    "² 8 * * *", "0 \u0661 * * *",  # Unicode digits: isdigit() takes them, int() refuses "²"
])
def test_refused(cron):
    with pytest.raises(Unconvertible):
        cron_to_rrule(cron)
