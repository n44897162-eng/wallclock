"""Properties that must hold no matter what the schedule is.

These are the tests that would catch a wrong *algorithm* rather than a wrong
constant: window partitioning, contract monotonicity, self-diff, and a
brute-force minute-by-minute simulator used as an independent oracle for cron.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from wallclock.cron import parse_cron
from wallclock.diff import diff
from wallclock.schedule import Schedule
from wallclock.tz import Policy, get_zone
from wallclock.verify import horizon, simulate, verify

UTC = timezone.utc


def u(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _brute_force(cron_expr: str, zone_name: str, policy: Policy, lo: datetime, hi: datetime) -> list[datetime]:
    """Independent simulator: step through every wall-clock minute."""
    zone = get_zone(zone_name)
    cron = parse_cron(cron_expr)
    start = (zone.wall(lo) - timedelta(days=1)).replace(second=0, microsecond=0)
    end = zone.wall(hi) + timedelta(days=1)
    out: list[datetime] = []
    t = start
    while t < end:
        if t.minute in cron.minute and t.hour in cron.hour and cron.day_matches(t.date()):
            for res in zone.resolve(t, policy):
                if lo <= res.utc < hi:
                    out.append(res.utc)
        t += timedelta(minutes=1)
    return sorted(out)


@pytest.mark.parametrize(
    "expr,zone_name",
    [
        ("*/20 2 * * *", "Europe/Berlin"),
        ("30 2 * * *", "Europe/Berlin"),
        ("0 0 * * *", "America/Santiago"),
        ("15 2 * * *", "Australia/Lord_Howe"),
        ("0 */6 * * *", "Asia/Kathmandu"),
        ("0 12 * * *", "Pacific/Apia"),
        ("0 0 1 * 1", "UTC"),
        ("30 2 * * 0", "Europe/Dublin"),
    ],
)
def test_cron_expansion_matches_brute_force(expr: str, zone_name: str) -> None:
    if zone_name == "Pacific/Apia":
        lo, hi = u(2011, 12, 28), u(2012, 1, 2)
    else:
        lo, hi = u(2026, 3, 27), u(2026, 3, 31)
    for policy in (Policy(), Policy(gap="clamp_end"), Policy(gap="shift_forward", ambiguous="both")):
        schedule = Schedule.from_cron(expr, zone_name, policy=policy)
        assert [f.utc for f in schedule.fire(lo, hi)] == _brute_force(expr, zone_name, policy, lo, hi)


def test_window_partition_is_conservative() -> None:
    for zone_name in ("Europe/Berlin", "America/Santiago", "Australia/Lord_Howe", "UTC"):
        schedule = Schedule.from_rrule("FREQ=DAILY", "2026-01-01T02:30", zone_name)
        lo, hi = horizon(zone_name, "2026-01-01", "2027-01-01")
        mid = lo + (hi - lo) / 2
        whole = [f.utc for f in schedule.fire(lo, hi)]
        parts = [f.utc for f in schedule.fire(lo, mid)] + [f.utc for f in schedule.fire(mid, hi)]
        assert whole == parts


def test_max_gap_errors_are_monotone_in_the_limit() -> None:
    schedule = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    lo, hi = u(2026, 3, 1), u(2026, 4, 1)
    limits = ["20h", "22h", "24h", "26h", "48h", "72h"]
    counts = []
    for limit in limits:
        report = verify(schedule, [{"max_gap": limit}], lo, hi)
        counts.append(len([f for f in report.findings if f.code == "max_gap"]))
    # a wider allowance can never create more violations
    assert counts == sorted(counts, reverse=True)


def test_local_window_errors_are_monotone_in_the_window() -> None:
    schedule = Schedule.from_cron("30 3 * * *", "Europe/Berlin", anchor="utc")
    lo, hi = u(2026, 1, 1), u(2026, 7, 1)
    windows = ["02:00-05:00", "02:00-04:00", "03:00-04:00", "03:00-03:59"]
    counts = []
    for window in windows:
        report = verify(schedule, [{"local_window": window}], lo, hi)
        counts.append(len([f for f in report.findings if f.code == "local_window"]))
    assert counts == sorted(counts)


def test_self_diff_is_always_empty() -> None:
    rng = random.Random(3)
    for _ in range(20):
        minute = rng.randrange(60)
        hour = rng.randrange(24)
        zone_name = rng.choice(["UTC", "Europe/Berlin", "America/Santiago", "Asia/Kathmandu"])
        schedule = Schedule.from_cron(f"{minute} {hour} * * *", zone_name)
        lo, hi = horizon(zone_name, "2026-01-01", "2026-04-01")
        assert diff(schedule, schedule, lo, hi).changed == 0


def test_skipped_count_matches_the_gap_semantics() -> None:
    # One skipped wall-clock occurrence for a daily job in a spring-forward year.
    schedule = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    sim = simulate(schedule, u(2026, 1, 1), u(2027, 1, 1))
    assert len(sim.skipped) == 1
    assert sim.skipped[0] == datetime(2026, 3, 29, 2, 30)


def test_min_per_day_flags_a_day_that_never_existed() -> None:
    schedule = Schedule.from_cron("0 12 * * *", "Pacific/Apia")
    report = verify(schedule, [{"min_per_day": 1}], u(2011, 12, 20), u(2012, 1, 5))
    assert any(f.code == "min_per_day" and f.evidence["date"] == "2011-12-30" for f in report.findings)


def test_firings_are_sorted_and_unique_per_sequence() -> None:
    schedule = Schedule.from_cron("*/17 * * * *", "Europe/Berlin", policy=Policy(ambiguous="both"))
    firings = schedule.fire(u(2026, 10, 24), u(2026, 10, 26))
    assert firings == sorted(firings, key=lambda f: (f.utc, f.sequence))
    keys = {(f.utc, f.kind) for f in firings}
    assert len(keys) == len(firings)