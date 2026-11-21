"""Schedule tests: wall clock -> instants, plus a croniter differential test."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from itertools import islice

import pytest

from wallclock.cron import parse_cron
from wallclock.errors import HorizonError, SpecError
from wallclock.schedule import Schedule
from wallclock.tz import Policy, get_zone

UTC = timezone.utc


def u(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# -- cron ------------------------------------------------------------------
def test_cron_in_utc_fires_daily() -> None:
    s = Schedule.from_cron("30 2 * * *", "UTC")
    got = s.fire(u(2026, 3, 1), u(2026, 3, 4))
    assert [f.utc for f in got] == [u(2026, 3, 1, 2, 30), u(2026, 3, 2, 2, 30), u(2026, 3, 3, 2, 30)]
    assert all(f.kind == "normal" for f in got)


def test_berlin_spring_forward_loses_the_job() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    got = s.fire(u(2026, 3, 28), u(2026, 3, 31))
    assert [f.utc for f in got] == [u(2026, 3, 28, 1, 30), u(2026, 3, 30, 0, 30)]


def test_berlin_spring_forward_with_clamp_policy() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin", policy=Policy(gap="clamp_end"))
    got = s.fire(u(2026, 3, 28), u(2026, 3, 31))
    assert len(got) == 3
    assert got[1].kind == "gap_clamp_end"
    assert got[1].utc == u(2026, 3, 29, 1, 0)
    assert got[1].effective_local == datetime(2026, 3, 29, 3, 0)


def test_berlin_fall_back_runs_twice_if_asked() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin", policy=Policy(ambiguous="both"))
    got = s.fire(u(2026, 10, 24), u(2026, 10, 26))
    # The window ends before 2026-10-26, and the 02:30 recurrence on the 25th
    # exists twice, so one wall-clock entry becomes two firings.
    assert [f.utc for f in got] == [
        u(2026, 10, 24, 0, 30),
        u(2026, 10, 25, 0, 30),
        u(2026, 10, 25, 1, 30),
    ]
    assert [f.kind for f in got][1:] == ["ambiguous_earlier", "ambiguous_later"]


def test_utc_anchor_drifts_against_local_clock() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin", anchor="utc")
    got = s.fire(u(2026, 1, 1), u(2026, 7, 1))
    local_times = {(f.effective_local.hour, f.effective_local.minute) for f in got}
    assert local_times == {(3, 30), (4, 30)}  # 02:30 UTC = 03:30 CET = 04:30 CEST
    assert all(f.kind == "utc" for f in got)


def test_santiago_midnight_job_is_skipped() -> None:
    s = Schedule.from_cron("0 0 * * *", "America/Santiago")
    got = s.fire(u(2026, 9, 5, 12), u(2026, 9, 7, 12))
    assert u(2026, 9, 6, 4, 0) not in [f.utc for f in got]
    assert len(got) == 1


def test_apia_loses_a_whole_local_day() -> None:
    s = Schedule.from_cron("0 12 * * *", "Pacific/Apia")
    got = s.fire(u(2011, 12, 28), u(2012, 1, 1))
    # 2011-12-30 never happened in Samoa; the next run lands on 2012-01-01
    # local time (still inside the UTC window).
    assert [f.effective_local.date().isoformat() for f in got] == [
        "2011-12-28", "2011-12-29", "2011-12-31", "2012-01-01",
    ]
    assert "2011-12-30" not in [f.effective_local.date().isoformat() for f in got]


# -- rrule -----------------------------------------------------------------
def test_rrule_daily_keeps_local_time_across_dst() -> None:
    s = Schedule.from_rrule("FREQ=DAILY", "2026-03-27T02:30", "Europe/Berlin")
    got = s.fire(u(2026, 3, 27), u(2026, 3, 31))
    assert [f.utc for f in got] == [u(2026, 3, 27, 1, 30), u(2026, 3, 28, 1, 30), u(2026, 3, 30, 0, 30)]
    assert [f.effective_local.hour for f in got] == [2, 2, 2]


def test_rrule_count_counts_recurrences_not_firings() -> None:
    s = Schedule.from_rrule("FREQ=DAILY;COUNT=3", "2026-03-28T02:30", "Europe/Berlin")
    got = s.fire(u(2026, 3, 27), u(2026, 4, 2))
    # 2026-03-29 02:30 does not exist, so COUNT=3 produces only two runs.
    assert len(got) == 2


def test_until_with_z_is_converted_to_wall_clock() -> None:
    s = Schedule.from_rrule("FREQ=DAILY;UNTIL=20260331T000000Z", "2026-03-27T02:30", "Europe/Berlin")
    assert s.rrule is not None
    assert s.rrule.until == datetime(2026, 3, 31, 2, 0)
    got = s.fire(u(2026, 3, 27), u(2026, 4, 2))
    assert [f.local.date().isoformat() for f in got] == [
        "2026-03-27", "2026-03-28", "2026-03-30",
    ]


def test_rrule_monthly_on_the_31st_skips_short_months() -> None:
    s = Schedule.from_rrule("FREQ=MONTHLY;BYMONTHDAY=31", "2026-01-31T23:00", "UTC")
    got = s.fire(u(2026, 1, 1), u(2026, 8, 1))
    assert [f.local.month for f in got] == [1, 3, 5, 7]


# -- plumbing --------------------------------------------------------------
def test_window_is_half_open() -> None:
    s = Schedule.from_cron("0 12 * * *", "UTC")
    assert len(s.fire(u(2026, 1, 1, 12), u(2026, 1, 2, 12))) == 1


def test_from_dict_roundtrip() -> None:
    spec = {
        "cron": "30 2 * * *",
        "timezone": "Europe/Berlin",
        "policy": {"gap": "clamp_end", "ambiguous": "both"},
    }
    s = Schedule.from_dict(spec)
    assert s.to_dict() == {**spec, "anchor": "local"}
    assert s.policy.gap == "clamp_end" and s.policy.ambiguous == "both"


def test_from_dict_requires_exactly_one_rule() -> None:
    with pytest.raises(SpecError):
        Schedule.from_dict({"timezone": "UTC"})
    with pytest.raises(SpecError):
        Schedule.from_dict({"timezone": "UTC", "cron": "* * * * *", "rrule": "FREQ=DAILY"})


def test_from_dict_rrule_needs_dtstart() -> None:
    with pytest.raises(SpecError):
        Schedule.from_dict({"timezone": "UTC", "rrule": "FREQ=DAILY"})


def test_warnings_explain_cron_or_semantics() -> None:
    s = Schedule.from_cron("0 0 1 * 1", "UTC")
    assert any("OR'd" in w for w in s.warnings())


def test_warnings_explain_unsynchronised_dtstart() -> None:
    s = Schedule.from_rrule("FREQ=WEEKLY;BYDAY=FR", "2026-01-05T09:00", "UTC")  # a Monday
    assert any("DTSTART does not satisfy" in w for w in s.warnings())


def test_fire_rejects_naive_bounds_and_huge_results() -> None:
    s = Schedule.from_cron("* * * * *", "UTC")
    with pytest.raises(HorizonError):
        s.fire(datetime(2026, 1, 1), u(2026, 1, 2))
    with pytest.raises(HorizonError):
        s.fire_all(u(2026, 1, 1), u(2026, 3, 1), limit=1000)


def test_describe_mentions_zone_and_policy() -> None:
    text = Schedule.from_cron("30 2 * * *", "Europe/Berlin").describe()
    assert "Europe/Berlin" in text and "gap=skip" in text


# -- differential ----------------------------------------------------------
def _random_cron(rng: random.Random) -> str:
    def field(values: list[str]) -> str:
        style = rng.random()
        if style < 0.3:
            return "*"
        if style < 0.45:
            return f"*/{rng.choice([2, 3, 4, 5])}"
        if style < 0.7:
            return ",".join(rng.sample(values, rng.randint(1, 3)))
        lo, hi = sorted(rng.sample(range(len(values)), 2))
        return f"{values[lo]}-{values[hi]}"

    return " ".join(
        [
            field([str(i) for i in range(0, 60, 5)]),
            field([str(i) for i in range(0, 24, 3)]),
            field([str(i) for i in range(1, 32)]),
            field(["1", "2", "3", "6", "9", "12"]),
            field(["0", "1", "2", "3", "4", "5", "6"]),
        ]
    )


def test_cron_differential_against_croniter() -> None:
    croniter_mod = pytest.importorskip("croniter")
    rng = random.Random(7)
    start = datetime(2026, 1, 1)
    end = start + timedelta(days=60)
    checked = 0
    while checked < 150:
        expr = _random_cron(rng)
        parts = expr.split()
        dom, dow = parts[2], parts[4]
        # Documented implementation split: Vixie flags a field as "starred" when
        # it *starts* with '*', so `*/2` turns DOM/DOW into AND; croniter keeps
        # OR.  test_croniter_and_vixie_split pins that divergence separately.
        if (dom.startswith("*") and dom != "*" and dow != "*") or (
            dow.startswith("*") and dow != "*" and dom != "*"
        ):
            continue
        mine = list(islice(parse_cron(expr).iter_local(start, end), 50))
        # croniter.get_next() is strictly after the cursor; back up a second so
        # both sides are half-open on the same boundary.
        it = croniter_mod.croniter(expr, start - timedelta(seconds=1))
        theirs = [it.get_next(datetime) for _ in range(len(mine))]
        assert mine == theirs, expr
        checked += 1
    assert checked == 150


def test_croniter_and_vixie_split_on_starred_steps() -> None:
    """Pin the known disagreement instead of pretending it does not exist."""
    croniter_mod = pytest.importorskip("croniter")
    expr = "0 0 */2 * 1"
    mine = list(parse_cron(expr).iter_local(datetime(2026, 1, 1), datetime(2026, 2, 1)))
    it = croniter_mod.croniter(expr, datetime(2026, 1, 1))
    theirs = [it.get_next(datetime) for _ in range(len(mine))]
    assert mine != theirs  # Vixie ANDs here, croniter ORs
    assert all(d.weekday() == 0 and d.day % 2 == 1 for d in mine)