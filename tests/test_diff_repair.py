"""Semantic diff and repair-synthesis tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from wallclock.diff import diff
from wallclock.repair import repair
from wallclock.schedule import Schedule
from wallclock.tz import Policy
from wallclock.verify import format_duration, verify

UTC = timezone.utc


def u(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# -- diff ------------------------------------------------------------------
def test_diff_of_identical_schedules() -> None:
    a = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    d = diff(a, a, u(2026, 7, 1), u(2026, 8, 1))
    assert d.changed == 0
    assert len(d.common) == 31
    assert "31 unchanged" in d.summary()


def test_diff_reports_a_moved_run() -> None:
    a = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    b = Schedule.from_cron("30 3 * * *", "Europe/Berlin")
    d = diff(a, b, u(2026, 7, 1), u(2026, 8, 1))
    assert len(d.moved) == 31
    assert {delta for _, _, delta in [(x, y, y.utc - x.utc) for x, y in d.moved]} == {timedelta(hours=1)}
    assert "31 moved" in d.summary()


def test_diff_reports_added_runs() -> None:
    a = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    b = Schedule.from_cron("30 2,14 * * *", "Europe/Berlin")
    d = diff(a, b, u(2026, 7, 1), u(2026, 8, 1))
    assert len(d.common) == 31 and len(d.added) == 31
    assert not d.removed and not d.moved


def test_diff_catches_a_policy_change_that_saves_one_run() -> None:
    a = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    b = Schedule.from_cron("30 2 * * *", "Europe/Berlin", policy=Policy(gap="clamp_end"))
    d = diff(a, b, u(2026, 3, 28), u(2026, 3, 31))
    assert len(d.added) == 1
    assert d.added[0].utc == u(2026, 3, 29, 1, 0)
    assert len(d.removed) == 0


def test_diff_to_dict_is_json() -> None:
    a = Schedule.from_cron("0 1 * * *", "UTC")
    b = Schedule.from_cron("0 2 * * *", "UTC")
    blob = json.dumps(diff(a, b, u(2026, 1, 1), u(2026, 2, 1)).to_dict())
    assert '"moved": 31' in blob


def test_diff_requires_aware_bounds() -> None:
    a = Schedule.from_cron("0 1 * * *", "UTC")
    with pytest.raises(Exception):
        diff(a, a, datetime(2026, 1, 1), u(2026, 2, 1))


# -- repair ----------------------------------------------------------------
def test_repair_finds_a_cheap_fix_for_the_lost_hour() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    lo, hi = u(2026, 3, 27), u(2026, 3, 31)
    assert not verify(s, [{"min_per_day": 1}], lo, hi).ok
    fixes = repair(s, [{"min_per_day": 1}], lo, hi)
    assert fixes and fixes[0].ok
    assert fixes[0].cost == 1
    # the fix must survive an independent re-verification
    assert verify(fixes[0].schedule, [{"min_per_day": 1}], lo, hi).ok


def test_repair_finds_a_fix_for_the_two_day_hole() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    lo, hi = u(2026, 3, 27), u(2026, 4, 2)
    contracts = [{"max_gap": "24h"}]
    assert not verify(s, contracts, lo, hi).ok
    fixes = repair(s, contracts, lo, hi)
    assert fixes[0].ok
    assert fixes[0].cost == 1
    # every candidate's embedded verdict must match an independent re-check
    assert all(f.report.ok == verify(f.schedule, contracts, lo, hi).ok for f in fixes)


def test_repair_handles_a_midnight_job_in_santiago() -> None:
    s = Schedule.from_cron("0 0 * * *", "America/Santiago")
    lo, hi = u(2026, 9, 1), u(2026, 10, 1)
    contracts = [{"min_per_day": 1}]
    fixes = repair(s, contracts, lo, hi)
    assert fixes and fixes[0].ok


def test_repair_says_nothing_can_fix_an_impossible_rule() -> None:
    s = Schedule.from_cron("0 0 30 2 *", "UTC")
    fixes = repair(s, [{"min_per_day": 1}], u(2026, 1, 1), u(2026, 3, 1))
    assert fixes
    assert not any(f.ok for f in fixes)


def test_repair_mentions_utc_anchoring_as_a_last_resort() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    fixes = repair(s, [{"max_gap": "24h"}], u(2026, 3, 27), u(2026, 4, 2))
    anchors = [f for f in fixes if f.schedule.anchor == "utc"]
    assert anchors, [f.description for f in fixes]
    assert anchors[0].cost == 3
    # UTC anchoring fixes the hole, but it cannot satisfy a local-time window
    assert anchors[0].report.ok


def test_repair_candidates_are_deduplicated() -> None:
    s = Schedule.from_cron("30 2 * * *", "UTC")
    fixes = repair(s, [{"max_gap": "25h"}], u(2026, 1, 1), u(2026, 2, 1))
    keys = [json.dumps(f.schedule.to_dict(), sort_keys=True) for f in fixes]
    assert len(keys) == len(set(keys))


def test_format_duration_examples() -> None:
    assert format_duration(timedelta(hours=23, minutes=30)) == "23h30m"