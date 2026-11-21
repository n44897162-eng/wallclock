"""Verifier tests: contracts, anomalies, and the report surface."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

import pytest

from wallclock.errors import HorizonError, SpecError
from wallclock.schedule import Schedule
from wallclock.verify import (
    MaxGap,
    MinPerDay,
    detect_anomalies,
    format_duration,
    horizon,
    parse_contract,
    parse_duration,
    parse_local_window,
    simulate,
    verify,
)

UTC = timezone.utc


def u(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def codes(report) -> list[str]:
    return [f.code for f in report.findings]


# -- parsing ---------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [("30s", 30), ("90m", 5400), ("1d2h", 93600), ("1.5h", 5400), ("26h", 93600)],
)
def test_parse_duration(text: str, expected: int) -> None:
    assert parse_duration(text) == timedelta(seconds=expected)


@pytest.mark.parametrize("text", ["", "h", "26", "1x", "1h2", "-3h"])
def test_bad_durations(text: str) -> None:
    with pytest.raises(SpecError):
        parse_duration(text)


def test_format_duration() -> None:
    assert format_duration(timedelta(hours=25)) == "1d1h"
    assert format_duration(timedelta(minutes=90)) == "1h30m"
    assert format_duration(timedelta(seconds=45)) == "45s"
    assert format_duration(timedelta(0)) == "0s"


def test_parse_local_window() -> None:
    assert parse_local_window("08:00-10:00") == (time(8), time(10))
    with pytest.raises(SpecError):
        parse_local_window("22:00-02:00")  # crossing midnight is not modelled
    with pytest.raises(SpecError):
        parse_local_window("8-10")


def test_parse_contract_rejects_unknown() -> None:
    with pytest.raises(SpecError):
        parse_contract({"max_jitter": "1h"})
    with pytest.raises(SpecError):
        parse_contract({"max_gap": "1h", "min_per_day": 1})


# -- contracts -------------------------------------------------------------
def test_max_gap_passes_with_48h_on_a_spring_forward_weekend() -> None:
    # A daily 02:30 job skips 2026-03-29 entirely, so two consecutive runs are
    # 47 hours apart -- the number that makes "daily" a lie.
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    report = verify(s, [{"max_gap": "48h"}], u(2026, 3, 27), u(2026, 3, 31))
    assert report.ok, [f.message for f in report.findings]


def test_max_gap_24h_fails_on_a_spring_forward_weekend() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    report = verify(s, [{"max_gap": "24h"}], u(2026, 3, 27), u(2026, 3, 31))
    assert not report.ok
    assert codes(report).count("max_gap") == 1
    assert "1d23h" in report.errors[0].message


def test_min_per_day_catches_the_missing_day() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    report = verify(s, [{"min_per_day": 1}], u(2026, 3, 27), u(2026, 3, 31))
    assert not report.ok
    missing = [f for f in report.findings if f.code == "min_per_day"]
    assert len(missing) == 1
    assert missing[0].evidence["date"] == "2026-03-29"


def test_local_window_catches_utc_anchored_drift() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin", anchor="utc")
    report = verify(s, [{"local_window": "08:00-09:00"}], u(2026, 1, 1), u(2026, 7, 1))
    assert not report.ok
    assert codes(report).count("local_window") == 41  # 40 detailed + one "and N more"


def test_local_window_passes_for_a_normal_job() -> None:
    s = Schedule.from_cron("30 8 * * *", "Europe/Berlin")
    report = verify(s, [{"local_window": "08:00-09:00"}], u(2026, 1, 1), u(2026, 7, 1))
    assert report.ok


def test_max_drift_flags_clamped_runs() -> None:
    from wallclock.tz import Policy

    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin", policy=Policy(gap="clamp_end"))
    report = verify(s, [{"max_drift": "0s"}], u(2026, 3, 28), u(2026, 3, 31))
    drift = [f for f in report.findings if f.code == "max_drift"]
    assert len(drift) == 1
    assert drift[0].evidence["drift_seconds"] == 1800


def test_max_per_day_catches_double_fire() -> None:
    from wallclock.tz import Policy

    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin", policy=Policy(ambiguous="both"))
    report = verify(s, [{"max_per_day": 1}], u(2026, 10, 24), u(2026, 10, 27))
    assert any(f.code == "max_per_day" and f.evidence["date"] == "2026-10-25" for f in report.findings)


# -- anomalies -------------------------------------------------------------
def test_skipped_occurrence_is_reported_with_the_transition() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    report = verify(s, [], u(2026, 3, 28), u(2026, 3, 31))
    skipped = [f for f in report.findings if f.code == "skipped_occurrence"]
    assert len(skipped) == 1
    assert "does not exist" in skipped[0].message
    assert skipped[0].at == u(2026, 3, 29, 1)


def test_ambiguous_occurrence_is_info_by_default() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    report = verify(s, [], u(2026, 10, 24), u(2026, 10, 27))
    assert any(f.code == "ambiguous_occurrence" and f.severity == "info" for f in report.findings)


def test_double_fire_under_both_policy() -> None:
    from wallclock.tz import Policy

    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin", policy=Policy(ambiguous="both"))
    report = verify(s, [], u(2026, 10, 24), u(2026, 10, 27))
    assert any(f.code == "double_fire" and f.severity == "warning" for f in report.findings)


def test_date_shift_is_reported() -> None:
    s = Schedule.from_cron("0 12 * * *", "Pacific/Apia", anchor="utc")
    report = verify(s, [], u(2026, 1, 1), u(2026, 1, 5))
    assert any(f.code == "date_shift" for f in report.findings)


def test_utc_jitter_is_reported_for_regular_jobs() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    report = verify(s, [], u(2026, 3, 27), u(2026, 4, 2))
    jitter = [f for f in report.findings if f.code == "utc_jitter"]
    assert len(jitter) == 1  # one 47h hole, then the 24h rhythm resumes
    assert jitter[0].evidence["gap_seconds"] == 47 * 3600


def test_never_fires_on_an_impossible_cron() -> None:
    s = Schedule.from_cron("0 0 30 2 *", "UTC")
    report = verify(s, [], u(2026, 1, 1), u(2027, 1, 1))
    assert any(f.code == "never_fires" and f.severity == "error" for f in report.findings)


def test_cron_or_extra_is_reported_with_examples() -> None:
    s = Schedule.from_cron("0 0 1 * 1", "UTC")
    report = verify(s, [], u(2026, 1, 1), u(2026, 4, 1))
    extra = [f for f in report.findings if f.code == "cron_or_extra"]
    assert len(extra) == 1
    assert extra[0].evidence["extra_runs"] > 0


def test_spec_warning_is_surfaced_as_a_finding() -> None:
    s = Schedule.from_rrule("FREQ=WEEKLY;BYDAY=FR", "2026-01-05T09:00", "UTC")
    report = verify(s, [], u(2026, 1, 1), u(2026, 2, 1))
    assert any(f.code == "spec" for f in report.findings)


# -- plumbing --------------------------------------------------------------
def test_simulate_counts_and_skips() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    sim = simulate(s, u(2026, 3, 27), u(2026, 3, 31))
    assert len(sim.firings) == 3
    assert sim.skipped == [datetime(2026, 3, 29, 2, 30)]
    assert len(sim.transitions) == 1


def test_report_is_json_serialisable() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    report = verify(s, [{"min_per_day": 1}], u(2026, 3, 27), u(2026, 3, 31))
    blob = json.dumps(report.to_dict())
    assert '"ok": false' in blob and "skipped_occurrence" in blob


def test_report_summary_and_by_code() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    report = verify(s, [{"min_per_day": 1}], u(2026, 3, 27), u(2026, 3, 31))
    assert report.summary().startswith("FAIL")
    assert report.by_code()["min_per_day"] == 1


def test_horizon_converts_local_dates() -> None:
    lo, hi = horizon("Europe/Berlin", "2026-03-01", "2026-04-01")
    assert lo == u(2026, 2, 28, 23)
    assert hi == u(2026, 3, 31, 22)


def test_verify_requires_a_window() -> None:
    s = Schedule.from_cron("* * * * *", "UTC")
    with pytest.raises(HorizonError):
        verify(s, [])
    with pytest.raises(HorizonError):
        verify(s, [], u(2026, 1, 2), u(2026, 1, 1))


def test_contract_objects_can_be_passed_directly() -> None:
    s = Schedule.from_cron("0 12 * * *", "UTC")
    report = verify(s, [MaxGap(timedelta(hours=25)), MinPerDay(1)], u(2026, 1, 1), u(2026, 1, 10))
    assert report.ok
    assert [c.describe() for c in report.contracts] == ["max_gap: 1d1h", "min_per_day: 1"]

def test_min_per_day_ignores_days_before_dtstart_and_after_until() -> None:
    s = Schedule.from_rrule("FREQ=DAILY;UNTIL=20260331T000000Z", "2026-03-27T02:30", "Europe/Berlin")
    report = verify(s, [{"min_per_day": 1}], u(2026, 3, 25), u(2026, 4, 3))
    days = {f.evidence["date"] for f in report.findings if f.code == "min_per_day"}
    assert days == {"2026-03-29"}  # only the DST hole is a real violation


def test_min_per_day_still_flags_gaps_inside_a_counted_rule() -> None:
    s = Schedule.from_rrule("FREQ=DAILY;COUNT=4", "2026-03-27T02:30", "Europe/Berlin")
    report = verify(s, [{"min_per_day": 1}], u(2026, 3, 25), u(2026, 4, 3))
    assert any(f.code == "min_per_day" and f.evidence["date"] == "2026-03-29" for f in report.findings)


def test_skipped_occurrences_are_grouped_per_gap() -> None:
    s = Schedule.from_cron("* * * * *", "Europe/Berlin")  # every minute
    report = verify(s, [], u(2026, 3, 29), u(2026, 3, 30))
    skipped = [f for f in report.findings if f.code == "skipped_occurrence"]
    assert len(skipped) == 1
    assert skipped[0].evidence["count"] == 60
    assert "02:00..02:59" in skipped[0].message


def test_whole_skipped_day_is_one_finding() -> None:
    s = Schedule.from_cron("*/15 * * * *", "Pacific/Apia")
    report = verify(s, [], u(2011, 12, 29), u(2011, 12, 31))
    skipped = [f for f in report.findings if f.code == "skipped_occurrence"]
    assert len(skipped) == 1
    assert skipped[0].evidence["count"] == 96  # 24h at quarter-hour resolution


def test_fire_limit_is_enforced() -> None:
    from wallclock.errors import HorizonError

    s = Schedule.from_cron("* * * * *", "UTC")
    with pytest.raises(HorizonError, match="more than"):
        s.fire(u(2026, 1, 1), u(2026, 1, 2), limit=10)


def test_simulate_budget_is_enforced() -> None:
    from wallclock.errors import HorizonError

    s = Schedule.from_cron("0 * * * *", "UTC")
    with pytest.raises(HorizonError, match="more than"):
        verify(s, [], u(2026, 1, 1), u(2026, 2, 1), max_firings=100)


# -- weekly and forbidden-window contracts ---------------------------------
def test_min_per_week_passes_for_a_daily_job() -> None:
    s = Schedule.from_cron("0 12 * * *", "UTC")
    # exactly one full ISO week inside the window
    report = verify(s, [{"min_per_week": 7}], u(2026, 1, 5), u(2026, 1, 12))
    assert report.ok


def test_min_per_week_flags_a_weekly_job() -> None:
    s = Schedule.from_cron("0 9 * * 1", "UTC")  # Mondays only
    report = verify(s, [{"min_per_week": 2}], u(2026, 1, 1), u(2026, 2, 1))
    weeks = [f for f in report.findings if f.code == "min_per_week"]
    assert weeks and all(f.evidence["runs"] == 1 for f in weeks)


def test_partial_iso_weeks_are_not_judged() -> None:
    s = Schedule.from_cron("0 12 * * *", "UTC")
    # a 10-day window contains no complete ISO week (Mon..Sun)
    report = verify(s, [{"min_per_week": 7}], u(2026, 1, 1), u(2026, 1, 11))
    assert not [f for f in report.findings if f.code == "min_per_week"]


def test_max_per_week_flags_a_daily_job() -> None:
    s = Schedule.from_cron("0 12 * * *", "UTC")
    report = verify(s, [{"max_per_week": 6}], u(2026, 1, 5), u(2026, 1, 12))
    assert [f for f in report.findings if f.code == "max_per_week"][0].evidence["runs"] == 7


def test_weekly_contract_respects_finite_rules() -> None:
    s = Schedule.from_rrule("FREQ=DAILY;UNTIL=20260107T120000", "2026-01-01T12:00", "UTC")
    report = verify(s, [{"min_per_week": 7}], u(2026, 1, 1), u(2026, 2, 1))
    # the rule ends on Jan 7, so later weeks have no runs and are not judged
    assert not [f for f in report.findings if f.code == "min_per_week"]


def test_forbidden_window_catches_a_midnight_job() -> None:
    s = Schedule.from_cron("0 2 * * *", "UTC")
    report = verify(s, [{"forbidden_window": "00:00-04:00"}], u(2026, 1, 1), u(2026, 1, 8))
    assert len([f for f in report.findings if f.code == "forbidden_window"]) == 7


def test_forbidden_window_can_cross_midnight() -> None:
    s = Schedule.from_cron("0 2 * * *", "UTC")
    report = verify(s, [{"forbidden_window": "22:00-06:00"}], u(2026, 1, 1), u(2026, 1, 4))
    assert len([f for f in report.findings if f.code == "forbidden_window"]) == 3


def test_forbidden_window_lets_a_safe_job_through() -> None:
    s = Schedule.from_cron("0 9 * * *", "UTC")
    report = verify(s, [{"forbidden_window": "00:00-04:00"}], u(2026, 1, 1), u(2026, 1, 8))
    assert report.ok


def test_forbidden_window_sees_utc_anchored_drift() -> None:
    s = Schedule.from_cron("0 2 * * *", "Europe/Berlin", anchor="utc")
    report = verify(s, [{"forbidden_window": "04:00-05:00"}], u(2026, 1, 1), u(2026, 12, 31))
    hits = [f for f in report.findings if f.code == "forbidden_window"]
    assert hits and len(hits) < 200  # only the summer half lands at 04:30


def test_new_contract_validation() -> None:
    with pytest.raises(SpecError):
        parse_contract({"forbidden_window": "08:00-08:00"})
    with pytest.raises(SpecError):
        parse_contract({"min_per_week": -1})
    with pytest.raises(SpecError):
        parse_contract({"local_window": "22:00-02:00"})  # still refuses to wrap
