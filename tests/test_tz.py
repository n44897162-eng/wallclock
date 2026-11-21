"""Ground-truth tests for the timezone core.

Every expectation here is a fact about tzdata that can be checked by hand, and
several of them are the "surprising" cases the whole project exists for:
Samoa skipping 2011-12-30, Lord Howe's 30-minute DST, Troll's two-hour jump.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from wallclock.errors import SpecError, ZoneError
from wallclock.tz import Policy, Zone, get_zone, tzdata_version

UTC = timezone.utc
H = timedelta(hours=1)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_berlin_has_exactly_two_transitions_in_2026() -> None:
    z = get_zone("Europe/Berlin")
    ts = z.transitions(utc(2026, 1, 1), utc(2027, 1, 1))
    assert [t.at for t in ts] == [utc(2026, 3, 29, 1), utc(2026, 10, 25, 1)]
    assert (ts[0].before, ts[0].after) == (H, 2 * H)
    assert (ts[1].before, ts[1].after) == (2 * H, H)


def test_transition_discovery_reports_gap_sign() -> None:
    z = get_zone("Europe/Berlin")
    spring, autumn = z.transitions(utc(2026, 1, 1), utc(2027, 1, 1))
    assert spring.is_gap and spring.gap == H
    assert not autumn.is_gap and autumn.gap == -H


def test_utc_has_no_transitions() -> None:
    assert get_zone("UTC").transitions(utc(2000, 1, 1), utc(2030, 1, 1)) == []


def test_lord_howe_half_hour_dst() -> None:
    z = get_zone("Australia/Lord_Howe")
    ts = z.transitions(utc(2026, 1, 1), utc(2027, 1, 1))
    assert len(ts) == 2
    assert ts[0].at == utc(2026, 4, 4, 15, 0)  # DST ends: 02:00 -> 01:30 (UTC+11 -> +10:30)
    assert ts[1].at == utc(2026, 10, 3, 15, 30)  # DST starts: 02:00 -> 02:30
    assert ts[1].gap == timedelta(minutes=30)


def test_troll_two_hour_jump() -> None:
    z = get_zone("Antarctica/Troll")
    ts = z.transitions(utc(2026, 1, 1), utc(2027, 1, 1))
    assert len(ts) == 2
    assert ts[0].at == utc(2026, 3, 29, 1)
    assert ts[0].gap == 2 * H


def test_samoa_skipped_a_whole_calendar_day() -> None:
    z = get_zone("Pacific/Apia")
    ts = z.transitions(utc(2011, 12, 1), utc(2012, 1, 1))
    assert len(ts) == 1
    tr = ts[0]
    assert tr.at == utc(2011, 12, 30, 10)
    assert (tr.before, tr.after) == (-10 * H, 14 * H)
    assert tr.gap == 24 * H


# -- resolution ------------------------------------------------------------
def test_normal_local_time_round_trips() -> None:
    z = get_zone("Europe/Berlin")
    (r,) = z.resolve(datetime(2026, 6, 1, 12, 0))
    assert r.kind == "normal" and r.utc == utc(2026, 6, 1, 10)
    assert z.wall(r.utc) == datetime(2026, 6, 1, 12, 0)


def test_nonexistent_time_under_all_policies() -> None:
    z = get_zone("Europe/Berlin")
    local = datetime(2026, 3, 29, 2, 30)
    assert z.resolve(local, Policy(gap="skip")) == []
    (fwd,) = z.resolve(local, Policy(gap="shift_forward"))
    (back,) = z.resolve(local, Policy(gap="shift_backward"))
    (clamp,) = z.resolve(local, Policy(gap="clamp_end"))
    assert fwd.utc == utc(2026, 3, 29, 1, 30) and z.wall(fwd.utc) == datetime(2026, 3, 29, 3, 30)
    assert back.utc == utc(2026, 3, 29, 0, 30) and z.wall(back.utc) == datetime(2026, 3, 29, 1, 30)
    assert clamp.utc == utc(2026, 3, 29, 1, 0) and z.wall(clamp.utc) == datetime(2026, 3, 29, 3, 0)
    assert fwd.kind == "gap_forward" and back.kind == "gap_backward" and clamp.kind == "gap_clamp_end"


def test_ambiguous_time_under_all_policies() -> None:
    z = get_zone("Europe/Berlin")
    local = datetime(2026, 10, 25, 2, 30)
    earlier = z.resolve(local, Policy(ambiguous="earlier"))
    later = z.resolve(local, Policy(ambiguous="later"))
    both = z.resolve(local, Policy(ambiguous="both"))
    assert len(earlier) == 1 and earlier[0].utc == utc(2026, 10, 25, 0, 30)
    assert earlier[0].offset == 2 * H
    assert len(later) == 1 and later[0].utc == utc(2026, 10, 25, 1, 30)
    assert later[0].offset == H
    assert [r.utc for r in both] == [utc(2026, 10, 25, 0, 30), utc(2026, 10, 25, 1, 30)]
    assert all(z.wall(r.utc) == local for r in both)


def test_clamp_end_for_the_whole_skipped_day_is_its_first_instant() -> None:
    z = get_zone("Pacific/Apia")
    (clamp,) = z.resolve(datetime(2011, 12, 30, 0, 0), Policy(gap="clamp_end"))
    assert clamp.utc == utc(2011, 12, 30, 10)
    assert z.wall(clamp.utc) == datetime(2011, 12, 31, 0, 0)


def test_lord_howe_quarter_past_skipped_time() -> None:
    z = get_zone("Australia/Lord_Howe")
    local = datetime(2026, 10, 4, 2, 15)
    assert z.resolve(local, Policy(gap="skip")) == []
    (fwd,) = z.resolve(local, Policy(gap="shift_forward"))
    assert z.wall(fwd.utc) == datetime(2026, 10, 4, 2, 45)


def test_santiago_midnight_does_not_exist_on_spring_forward() -> None:
    z = get_zone("America/Santiago")
    assert get_zone("America/Santiago").resolve(datetime(2026, 9, 6, 0, 0), Policy(gap="skip")) == []
    (clamp,) = z.resolve(datetime(2026, 9, 6, 0, 0), Policy(gap="clamp_end"))
    assert z.wall(clamp.utc) == datetime(2026, 9, 6, 1, 0)


def test_dublin_negative_dst_is_a_normal_overlap() -> None:
    z = get_zone("Europe/Dublin")
    local = datetime(2026, 10, 25, 1, 30)
    both = z.resolve(local, Policy(ambiguous="both"))
    assert [r.utc for r in both] == [utc(2026, 10, 25, 0, 30), utc(2026, 10, 25, 1, 30)]
    assert [r.offset for r in both] == [H, timedelta(0)]


def test_exists_and_fire_times_helpers() -> None:
    z = get_zone("Europe/Berlin")
    assert z.exists(datetime(2026, 6, 1, 0, 0))
    assert not z.exists(datetime(2026, 3, 29, 2, 30))
    assert z.fire_times(datetime(2026, 3, 29, 2, 30), Policy(gap="clamp_end")) == [utc(2026, 3, 29, 1)]


def test_policy_rejects_unknown_values() -> None:
    with pytest.raises(SpecError):
        Policy(gap="explode")
    with pytest.raises(SpecError):
        Policy(ambiguous="random")


def test_unknown_zone_is_a_friendly_error() -> None:
    with pytest.raises(ZoneError, match="unknown timezone"):
        Zone("Mars/Olympus_Mons")


def test_transitions_requires_ordered_aware_bounds() -> None:
    z = get_zone("UTC")
    with pytest.raises(SpecError):
        z.transitions(datetime(2026, 1, 1), datetime(2026, 2, 1))  # naive
    with pytest.raises(SpecError):
        z.transitions(utc(2026, 2, 1), utc(2026, 1, 1))


def test_transition_near_finds_the_last_one() -> None:
    z = get_zone("Europe/Berlin")
    tr = z.transition_near(utc(2026, 4, 15))
    assert tr is not None and tr.at == utc(2026, 3, 29, 1)


def test_tzdata_version_is_reported_when_available() -> None:
    v = tzdata_version()
    if v is not None:
        assert re.fullmatch(r"\d{4}[a-z]+", v), v