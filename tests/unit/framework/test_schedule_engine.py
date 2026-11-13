"""The schedule engine (AG-67 plan §5.1): RRULE fires, DST, anchors and the evaluation bounds."""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timedelta

import pytest

from agento.framework.schedule import engine
from agento.framework.schedule.engine import InvalidSchedule, next_fires, normalize, validate

WAW = "Europe/Warsaw"
IMPOSSIBLE_MINUTELY = "FREQ=MINUTELY;INTERVAL=7;BYHOUR=1,2,3;BYMONTH=2;BYMONTHDAY=30"


def _code(fn, *args, **kwargs) -> str:
    with pytest.raises(InvalidSchedule) as exc:
        fn(*args, **kwargs)
    return exc.value.code


class TestNormalize:
    @pytest.mark.parametrize("text, expected", [
        ("RRULE:FREQ=DAILY;BYHOUR=8", "FREQ=DAILY;BYHOUR=8"),
        ("freq=weekly;byday=mo", "FREQ=WEEKLY;BYDAY=MO"),
        ("  FREQ=DAILY\n", "FREQ=DAILY"),
    ])
    def test_accepts(self, text, expected):
        assert normalize(text) == expected

    @pytest.mark.parametrize("text", [
        "DTSTART:20260101T000000\nRRULE:FREQ=DAILY",
        "FREQ=DAILY\nEXDATE:20260102T000000",
        "TZID=Europe/Warsaw:FREQ=DAILY",
        "BYHOUR=8",                      # no FREQ
        "FREQ=FORTNIGHTLY",
        "FREQ=DAILY;FREQ=WEEKLY",
        "FREQ=DAILY;BYEASTER=0",         # a dateutil extension, not RFC 5545
        "FREQ=DAILY;BYSECOND=0,30",      # schedules are minute-grained
        "FREQ=DAILY;INTERVAL=0",
        "FREQ=DAILY;COUNT=x",
        "FREQ=DAILY;BYHOUR",
        "",
    ])
    def test_refuses(self, text):
        assert _code(normalize, text) == "bad_rule"


class TestBoundsAtParse:
    @pytest.mark.parametrize("rule, code", [
        ("FREQ=SECONDLY", "too_frequent"),
        ("FREQ=MINUTELY;INTERVAL=2", "too_frequent"),
        ("FREQ=DAILY;COUNT=10001", "bad_rule"),
        ("FREQ=DAILY;UNTIL=nonsense", "bad_rule"),
        ("FREQ=DAILY;BYMONTH=13", "bad_rule"),
    ])
    def test_refused(self, rule, code):
        assert _code(validate, rule, datetime(2026, 1, 1), "UTC",
                     min_interval_minutes=5, now=datetime(2026, 1, 1)) == code

    def test_minutely_with_a_byminute_list_is_held_to_the_gap_check(self):
        fires = validate("FREQ=MINUTELY;BYMINUTE=0,30", datetime(2026, 1, 1), "UTC",
                         min_interval_minutes=5, now=datetime(2026, 1, 1))
        assert [f.at_utc.minute for f in fires[:3]] == [30, 0, 30]

    def test_bad_timezone(self):
        assert _code(next_fires, "FREQ=DAILY", datetime(2026, 1, 1), "Mars/Base",
                     after=datetime(2026, 1, 1), n=1) == "bad_timezone"


class TestTimes:
    def test_local_wall_time_is_kept_across_dst(self):
        # 08:00 Warsaw is 07:00 UTC in winter, 06:00 UTC in summer.
        fires = next_fires("FREQ=DAILY;BYHOUR=8;BYMINUTE=0", datetime(2026, 3, 28), WAW,
                           after=datetime(2026, 3, 27), n=3)
        assert [f.at_utc for f in fires] == [
            datetime(2026, 3, 28, 7), datetime(2026, 3, 29, 6), datetime(2026, 3, 30, 6)]

    def test_spring_gap_moves_forward_and_keeps_the_naive_local(self):
        # 2026-03-29 02:30 does not exist in Warsaw: it fires at 03:30 local (01:30 UTC).
        fires = next_fires("FREQ=DAILY;BYHOUR=2;BYMINUTE=30", datetime(2026, 3, 28), WAW,
                           after=datetime(2026, 3, 28, 12), n=2)
        assert fires[0].at_utc == datetime(2026, 3, 29, 1, 30)
        assert fires[0].local == datetime(2026, 3, 29, 2, 30)
        assert fires[1].at_utc == datetime(2026, 3, 30, 0, 30)

    def test_spring_gap_never_fires_twice(self):
        # 02:00 and 02:30 move to 03:00/03:30, which the rule also makes: one fire each.
        fires = next_fires("FREQ=HOURLY;BYMINUTE=0,30", datetime(2026, 3, 29), WAW,
                           after=datetime(2026, 3, 28, 23, 0), n=6)
        at = [f.at_utc for f in fires]
        assert at == sorted(set(at))
        assert at == [datetime(2026, 3, 28, 23, 30), datetime(2026, 3, 29, 0, 0),
                      datetime(2026, 3, 29, 0, 30), datetime(2026, 3, 29, 1, 0),
                      datetime(2026, 3, 29, 1, 30), datetime(2026, 3, 29, 2, 0)]

    def test_autumn_ambiguous_time_takes_the_first(self):
        # 2026-10-25 02:30 happens twice in Warsaw; fold=0 is the first (00:30 UTC).
        fires = next_fires("FREQ=DAILY;BYHOUR=2;BYMINUTE=30", datetime(2026, 10, 24), WAW,
                           after=datetime(2026, 10, 24, 12), n=1)
        assert fires[0].at_utc == datetime(2026, 10, 25, 0, 30)

    def test_aware_after_is_taken_as_utc(self):
        from datetime import UTC
        fires = next_fires("FREQ=DAILY;BYHOUR=8;BYMINUTE=0", datetime(2026, 1, 1), "UTC",
                           after=datetime(2026, 1, 1, 9, tzinfo=UTC), n=1)
        assert fires[0].at_utc == datetime(2026, 1, 2, 8)

    def test_until_in_utc_is_read_against_the_local_start(self):
        fires = next_fires("FREQ=DAILY;BYHOUR=8;BYMINUTE=0;UNTIL=20260103T070000Z", datetime(2026, 1, 1),
                           WAW, after=datetime(2025, 12, 31), n=10)
        assert [f.local.day for f in fires] == [1, 2, 3]

    def test_after_until_returns_nothing_without_evaluating(self, mocker):
        spy = mocker.patch.object(engine, "rrulestr")
        assert next_fires("FREQ=DAILY;UNTIL=20260101", datetime(2025, 1, 1), "UTC",
                          after=datetime(2026, 6, 1), n=1) == []
        spy.assert_not_called()


class TestAnchor:
    @pytest.mark.parametrize("rule, start", [
        ("FREQ=DAILY;INTERVAL=2;BYHOUR=9;BYMINUTE=15", datetime(2026, 1, 1, 9, 15)),
        ("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE", datetime(2026, 1, 5, 10, 0)),
        ("FREQ=MONTHLY;BYDAY=-1FR", datetime(2026, 1, 1, 18, 0)),
        ("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29", datetime(2024, 1, 1, 7, 0)),
        ("FREQ=DAILY;BYHOUR=2;BYMINUTE=30", datetime(2026, 3, 20)),  # an anchor in the spring gap
    ])
    def test_re_anchoring_gives_the_same_next_20(self, rule, start):
        after = datetime(2026, 3, 1)
        from_start = next_fires(rule, start, WAW, after=after, n=40)
        # Anchor at several scheduled occurrences (index 9 of the gap rule is 2026-03-29),
        # then continue. The yearly rule has fewer fires within the horizon.
        for i in (i for i in (0, 3, 9) if i + 1 < len(from_start)):
            anchor = from_start[i]
            expected = from_start[i + 1:i + 21]
            again = next_fires(rule, start, WAW, after=anchor.at_utc, n=20, anchor_local=anchor.local)
            assert again[:len(expected)] == expected

    def test_spring_gap_anchor_keeps_its_naive_local(self):
        rule = "FREQ=DAILY;BYHOUR=2;BYMINUTE=30"
        gap = next(f for f in next_fires(rule, datetime(2026, 3, 20), WAW, after=datetime(2026, 3, 20), n=20)
                   if f.local.date() == datetime(2026, 3, 29).date())
        assert gap.local == datetime(2026, 3, 29, 2, 30)
        after = next_fires(rule, datetime(2026, 3, 20), WAW, after=gap.at_utc, n=2, anchor_local=gap.local)
        assert [f.local for f in after] == [datetime(2026, 3, 30, 2, 30), datetime(2026, 3, 31, 2, 30)]

    def test_run_now_leaves_the_phase_unchanged(self):
        # Run now writes last_fire_at, never the anchor: an off-phase moment is not an anchor,
        # so the next scheduled fires are the same as before it.
        rule, start = "FREQ=DAILY;INTERVAL=3;BYHOUR=9;BYMINUTE=0", datetime(2026, 1, 1, 9)
        anchor = next_fires(rule, start, "UTC", after=datetime(2026, 2, 1), n=1)[0]
        before = next_fires(rule, start, "UTC", after=anchor.at_utc, n=5, anchor_local=anchor.local)
        run_now = anchor.at_utc + timedelta(hours=26)
        after_run = next_fires(rule, start, "UTC", after=run_now, n=4, anchor_local=anchor.local)
        assert after_run == before[:4]

    def test_count_rules_ignore_the_anchor(self):
        rule = "FREQ=DAILY;COUNT=3"
        fires = next_fires(rule, datetime(2026, 1, 1), "UTC", after=datetime(2025, 1, 1), n=10,
                           anchor_local=datetime(2026, 1, 2))
        assert [f.local.day for f in fires] == [1, 2, 3]


class TestBounds:
    def test_old_minutely_start_is_too_far_back(self):
        assert _code(next_fires, "FREQ=MINUTELY;INTERVAL=5", datetime(2020, 1, 1), "UTC",
                     after=datetime(2026, 1, 1), n=1) == "too_far_back"

    def test_count_10000_finishes_within_the_window(self):
        t = time.monotonic()
        fires = next_fires("FREQ=HOURLY;COUNT=10000", datetime(2026, 1, 1), "UTC",
                           after=datetime(2026, 1, 1), n=10_000)
        assert len(fires) == 9_999  # the start itself is not after `after`
        assert time.monotonic() - t < 5

    @pytest.mark.parametrize("rule, start", [  # each start has an occurrence within the horizon
        ("FREQ=DAILY;BYMONTH=2;BYMONTHDAY=29;BYDAY=MO", datetime(2043, 1, 1)),
        ("FREQ=YEARLY;INTERVAL=7;BYMONTH=2;BYMONTHDAY=29", datetime(2036, 1, 1)),
        ("FREQ=MINUTELY;INTERVAL=7;BYHOUR=1,2,3;BYMONTH=2;BYMONTHDAY=29", datetime(2043, 1, 1)),
    ])
    def test_rare_valid_rules_are_fast_after_their_last_occurrence(self, rule, start):
        last = next_fires(rule, start, "UTC", after=start, n=1)[0]
        t = time.monotonic()
        next_fires(rule, start, "UTC", after=last.at_utc, n=1, anchor_local=last.local)
        assert time.monotonic() - t < 0.5


class TestValidate:
    NOW = datetime(2026, 1, 1, 12)

    def test_returns_the_next_five(self):
        fires = validate("FREQ=DAILY;BYHOUR=8;BYMINUTE=0", datetime(2026, 1, 1), "UTC",
                         min_interval_minutes=5, now=self.NOW)
        assert [f.at_utc for f in fires] == [datetime(2026, 1, d, 8) for d in range(2, 7)]
        assert all(f.local == f.at_utc for f in fires)

    def test_no_occurrence(self):
        assert _code(validate, "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30", datetime(2026, 1, 1), "UTC",
                     min_interval_minutes=5, now=self.NOW) == "no_occurrence"

    def test_too_frequent_from_the_gaps(self):
        assert _code(validate, "FREQ=HOURLY;BYMINUTE=0,1", datetime(2026, 1, 1), "UTC",
                     min_interval_minutes=5, now=self.NOW) == "too_frequent"

    def test_too_far_back(self):
        assert _code(validate, "FREQ=MINUTELY;INTERVAL=5", datetime(2020, 1, 1), "UTC",
                     min_interval_minutes=5, now=self.NOW) == "too_far_back"

    def test_impossible_rule_is_too_complex_and_leaves_no_child(self, mocker):
        pids: list[int] = []
        real = subprocess.Popen

        class Recording(real):  # type: ignore[misc, valid-type]
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                pids.append(self.pid)

        mocker.patch.object(engine.subprocess, "Popen", Recording)
        deadline = 0.5
        t = time.monotonic()
        assert _code(validate, IMPOSSIBLE_MINUTELY, datetime(2026, 1, 1), "UTC",
                     min_interval_minutes=5, now=self.NOW, deadline=deadline) == "too_complex"
        assert time.monotonic() - t < deadline + 1
        assert len(pids) == 1
        with pytest.raises(ProcessLookupError):
            os.kill(pids[0], 0)

    def test_child_gets_only_path_and_pythonpath(self, mocker, monkeypatch):
        monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "secret")
        run = mocker.patch.object(engine.subprocess, "run", wraps=subprocess.run)
        validate("FREQ=DAILY", datetime(2026, 1, 1), "UTC", min_interval_minutes=5, now=self.NOW)
        assert set(run.call_args.kwargs["env"]) == {"PATH", "PYTHONPATH"}
        assert run.call_args.args[0][1:4] == ["-P", "-m", "agento.framework.schedule.engine"]

    @pytest.mark.parametrize("stdout, code", [("not json", 0), ('{"fires": []}', 1)])
    def test_a_broken_child_is_an_error_not_a_verdict(self, mocker, stdout, code):
        mocker.patch.object(engine.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], code, stdout=stdout, stderr=""))
        with pytest.raises(RuntimeError):
            validate("FREQ=DAILY", datetime(2026, 1, 1), "UTC", min_interval_minutes=5, now=self.NOW)
