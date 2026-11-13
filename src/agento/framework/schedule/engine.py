"""The schedule engine: RRULE text -> fire times (AG-67 plan §5.1). Pure, no DB.

Keep this module import-light (stdlib and dateutil only): ``validate()`` runs it as
``python -P -m agento.framework.schedule.engine`` in a child process, and the package
``__init__`` stays empty for the same reason.

Times: dateutil generates naive local times from a naive DTSTART; ``ZoneInfo(tz)`` is attached
after. A spring-gap time moves forward through the UTC round trip, an autumn-ambiguous time
takes ``fold=0``. ``Fire.local`` keeps the naive time the rule made, before that normalisation:
it is what an anchor stores. Every naive time this module takes or returns that is not
``local``/``starts_at``/``anchor_local`` is UTC (CODE-1).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

HORIZON = timedelta(days=5 * 366)
MAX_COUNT = 10_000
MAX_PERIODS = 100_000  # the work budget: FREQ periods one evaluation may span from its start
VALIDATE_DEADLINE_S = 2
GAP_SAMPLE = 50  # validate() checks the smallest gap among this many next fires
PREVIEW = 5

_MODULE = "agento.framework.schedule.engine"
# The shortest length of one period of each FREQ (plan §5.1 window).
_PERIOD = {
    "YEARLY": timedelta(days=365), "MONTHLY": timedelta(days=28), "WEEKLY": timedelta(days=7),
    "DAILY": timedelta(days=1), "HOURLY": timedelta(hours=1), "MINUTELY": timedelta(minutes=1),
    "SECONDLY": timedelta(seconds=1),
}
# Integer list parts and their ranges; True: a negative value counts from the end.
_INT_LISTS = {
    "BYMONTH": (1, 12, False), "BYMONTHDAY": (1, 31, True), "BYYEARDAY": (1, 366, True),
    "BYWEEKNO": (1, 53, True), "BYSETPOS": (1, 366, True), "BYHOUR": (0, 23, False),
    "BYMINUTE": (0, 59, False),
}
_PARTS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST", *_INT_LISTS}
# ASCII digits with one optional minus, as Node's /^-?\d+$/ (rrule_vectors.json): `\d` and
# str.isdigit() also take other Unicode digits, and int() refuses some of those ("²").
_INT = re.compile(r"-?[0-9]+")
_LATEST = datetime(9998, 12, 31)  # beyond this a window end is "never": UTC conversion would overflow


class InvalidSchedule(ValueError):
    """A refused rule. ``code``: no_occurrence | too_frequent | too_far_back | too_complex |
    bad_rule | bad_timezone."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Fire(NamedTuple):
    at_utc: datetime  # naive UTC
    local: datetime   # naive local, before DST normalisation


def normalize(rrule_text: str) -> str:
    """One canonical RRULE line: no ``RRULE:`` prefix, upper case, no DTSTART/TZID/EXDATE."""
    return ";".join(f"{k}={v}" for k, v in _parse(rrule_text).items())


def _parse(rrule_text: str) -> dict[str, str]:
    lines = [ln.strip() for ln in (rrule_text or "").strip().splitlines() if ln.strip()]
    if len(lines) != 1:
        raise InvalidSchedule("bad_rule")
    text = lines[0].upper()
    text = text.removeprefix("RRULE:")
    if ":" in text:  # DTSTART:, EXDATE:, TZID=…: — the start and zone are columns, not rule text
        raise InvalidSchedule("bad_rule")
    parts: dict[str, str] = {}
    for item in text.split(";"):
        key, sep, value = item.partition("=")
        if not sep or not value or key not in _PARTS or key in parts:
            raise InvalidSchedule("bad_rule")
        parts[key] = value
    if parts.get("FREQ") not in _PERIOD:
        raise InvalidSchedule("bad_rule")
    for key in ("INTERVAL", "COUNT"):
        if key in parts and not (_INT.fullmatch(parts[key]) and int(parts[key]) >= 1):
            raise InvalidSchedule("bad_rule")
    for key, (lo, hi, negative) in _INT_LISTS.items():
        for v in parts[key].split(",") if key in parts else ():
            n = int(v) if _INT.fullmatch(v) else None
            if n is None or not (lo <= (abs(n) if negative else n) <= hi) or (n < 0 and not negative):
                raise InvalidSchedule("bad_rule")
    if parts["FREQ"] == "SECONDLY":
        raise InvalidSchedule("too_frequent")
    if int(parts.get("COUNT", "1")) > MAX_COUNT:
        raise InvalidSchedule("bad_rule")
    return parts


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise InvalidSchedule("bad_timezone") from None


def _to_utc(local: datetime, zone: ZoneInfo) -> datetime:
    if local >= _LATEST:
        return _LATEST
    return local.replace(tzinfo=zone, fold=0).astimezone(UTC).replace(tzinfo=None)


def _naive_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is None else dt.astimezone(UTC).replace(tzinfo=None)


def _until_local(value: str, zone: ZoneInfo) -> datetime:
    """UNTIL as naive local: dateutil refuses a UTC UNTIL with a naive DTSTART."""
    try:
        if value.endswith("Z"):
            utc = datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
            return utc.astimezone(zone).replace(tzinfo=None)
        fmt = "%Y%m%dT%H%M%S" if "T" in value else "%Y%m%d"
        return datetime.strptime(value, fmt)
    except ValueError:
        raise InvalidSchedule("bad_rule") from None


def next_fires(rrule: str, starts_at: datetime, tz: str, *, after: datetime, n: int,
               anchor_local: datetime | None = None) -> list[Fire]:
    """The next ``n`` fires strictly after ``after`` (UTC), in time order, without duplicates.

    Run in-process only for a validated rule (plan §5.1 part 2): a rule that was never proved
    to have an occurrence can scan to year 9999, which only ``validate()``'s deadline bounds.
    ``anchor_local`` (a scheduled occurrence, naive local) replaces ``starts_at`` as the
    evaluation start for rules without COUNT; it keeps every DTSTART default and the INTERVAL
    phase, because the rule itself generated it.
    """
    parts = _parse(rrule)
    zone = _zone(tz)
    after = _naive_utc(after)
    d0 = anchor_local if anchor_local is not None and "COUNT" not in parts else starts_at
    if "UNTIL" in parts:
        until = _until_local(parts["UNTIL"], zone)
        if _to_utc(until, zone) <= after:
            return []
        parts["UNTIL"] = until.strftime("%Y%m%dT%H%M%S")
    try:
        span_end = min(d0 + MAX_PERIODS * int(parts.get("INTERVAL", "1")) * _PERIOD[parts["FREQ"]], _LATEST)
    except OverflowError:
        span_end = _LATEST
    if _to_utc(span_end, zone) <= after:
        raise InvalidSchedule("too_far_back")
    end = min(_to_utc(span_end, zone), after + HORIZON)
    try:
        rule = rrulestr(";".join(f"{k}={v}" for k, v in parts.items()), dtstart=d0)
    except (ValueError, TypeError):
        raise InvalidSchedule("bad_rule") from None

    out: list[Fire] = []
    last: datetime | None = None
    for local in rule:
        at = _to_utc(local, zone)
        if at > end:
            break
        # A spring-gap time moves forward onto a time the rule may make itself: one fire.
        if last is not None and at <= last:
            continue
        last = at
        if at > after:
            out.append(Fire(at, local))
            if len(out) >= n:
                break
    return out


def _check(rrule: str, starts_at: datetime, tz: str, min_interval_minutes: int, now: datetime) -> list[Fire]:
    """``validate()``'s work, run in the child."""
    parts = _parse(rrule)
    if (parts["FREQ"] == "MINUTELY" and "BYMINUTE" not in parts
            and int(parts.get("INTERVAL", "1")) < min_interval_minutes):
        raise InvalidSchedule("too_frequent")
    fires = next_fires(rrule, starts_at, tz, after=now, n=GAP_SAMPLE)
    if not fires:
        raise InvalidSchedule("no_occurrence")
    gap = timedelta(minutes=min_interval_minutes)
    if any(b.at_utc - a.at_utc < gap for a, b in pairwise(fires)):
        raise InvalidSchedule("too_frequent")
    return fires[:PREVIEW]


def validate(rrule: str, starts_at: datetime, tz: str, *, min_interval_minutes: int, now: datetime,
             deadline: float = VALIDATE_DEADLINE_S) -> list[Fire]:
    """The next 5 fires after ``now``, evaluated from ``starts_at``, or ``InvalidSchedule``.

    Runs in a child process under a hard deadline: dateutil checks UNTIL only on a candidate,
    so a rule with no candidate scans to year 9999 whatever its UNTIL says. ``-P`` keeps the
    cwd off ``sys.path`` but keeps ``PYTHONPATH``, which the images need to load the mounted
    package. The child gets only PATH and PYTHONPATH: no secret of this process (SEC-1).
    A child that crashes is an error, never a verdict on the rule.
    """
    payload = json.dumps({
        "rrule": rrule, "starts_at": starts_at.isoformat(), "tz": tz,
        "min_interval_minutes": min_interval_minutes, "now": _naive_utc(now).isoformat(),
    })
    env = {"PATH": os.environ.get("PATH", os.defpath), "PYTHONPATH": os.environ.get("PYTHONPATH", "")}
    try:
        proc = subprocess.run([sys.executable, "-P", "-m", _MODULE], input=payload, capture_output=True,
                              text=True, env=env, timeout=deadline, check=False)
    except subprocess.TimeoutExpired:  # run() has killed and reaped the child
        raise InvalidSchedule("too_complex") from None
    try:
        if proc.returncode != 0:
            raise ValueError(proc.returncode)
        result = json.loads(proc.stdout)
        if "error" in result:
            raise InvalidSchedule(str(result["error"]))
        return [Fire(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in result["fires"]]
    except InvalidSchedule:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"schedule validator failed (exit {proc.returncode})") from exc


def _main() -> None:
    args = json.loads(sys.stdin.read())
    try:
        fires = _check(args["rrule"], datetime.fromisoformat(args["starts_at"]), args["tz"],
                       int(args["min_interval_minutes"]), datetime.fromisoformat(args["now"]))
        out: dict = {"fires": [[f.at_utc.isoformat(), f.local.isoformat()] for f in fires]}
    except InvalidSchedule as exc:
        out = {"error": exc.code}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    _main()
