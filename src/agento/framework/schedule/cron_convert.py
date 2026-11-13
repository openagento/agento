"""cron expression -> RRULE (AG-67 plan §5.2), for the one-time migration of cron schedules.

Each of the five fields becomes a ``BY*`` list. FREQ is the period of the coarsest restricted
day-or-time field; a restricted month is a ``BYMONTH`` filter. Every field finer than FREQ is
written out, ``*`` included, because RRULE takes a missing one from DTSTART.
"""
from __future__ import annotations

import re

# (low, high) of minute, hour, day of month, month, day of week (0 and 7 are Sunday)
_FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_DAYS = ("SU", "MO", "TU", "WE", "TH", "FR", "SA")


class Unconvertible(ValueError):
    """A cron expression with no RRULE twin, or a malformed one."""


def _int(text: str, lo: int, hi: int) -> int:
    if not re.fullmatch(r"[0-9]+", text) or not lo <= int(text) <= hi:  # ASCII: isdigit() takes "²"
        raise Unconvertible(f"value {text!r} is not in {lo}-{hi}")
    return int(text)


def _expand(field: str, lo: int, hi: int) -> list[int]:
    values: set[int] = set()
    for item in field.split(","):
        rng, _, step_text = item.partition("/")
        step = _int(step_text, 1, hi) if step_text else 1
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a_text, _, b_text = rng.partition("-")
            a, b = _int(a_text, lo, hi), _int(b_text, lo, hi)
            if a > b:
                raise Unconvertible(f"range {rng!r} runs backwards")
        elif step_text:
            raise Unconvertible(f"step on a single value {item!r}")
        else:
            a = b = _int(rng, lo, hi)
        values.update(range(a, b + 1, step))
    return sorted(values)


def cron_to_rrule(expr: str) -> str:
    fields = expr.split()
    if len(fields) != 5:
        raise Unconvertible("a cron expression has 5 fields")
    minute, hour, dom, month, dow = (
        _expand(f, lo, hi) for f, (lo, hi) in zip(fields, _FIELDS, strict=True))
    star = [f == "*" for f in fields]
    if not star[2] and not star[4]:
        raise Unconvertible("day of month and day of week both restricted (cron ORs them)")

    def nums(values: list[int]) -> str:
        return ",".join(map(str, values))

    if not star[2]:
        parts = ["FREQ=MONTHLY"]
    elif not star[4]:
        parts = ["FREQ=WEEKLY"]
    elif not star[1]:
        parts = ["FREQ=DAILY"]
    elif not star[0]:
        parts = ["FREQ=HOURLY"]
    else:
        parts = ["FREQ=MINUTELY"]
    freq = parts[0]
    if not star[3]:
        parts.append(f"BYMONTH={nums(month)}")
    if not star[2]:
        parts.append(f"BYMONTHDAY={nums(dom)}")
    if not star[4]:
        parts.append("BYDAY=" + ",".join(dict.fromkeys(_DAYS[d % 7] for d in dow)))
    if freq in ("FREQ=MONTHLY", "FREQ=WEEKLY", "FREQ=DAILY"):
        parts.append(f"BYHOUR={nums(hour)}")
    if freq != "FREQ=MINUTELY":
        parts.append(f"BYMINUTE={nums(minute)}")
    return ";".join(parts)
