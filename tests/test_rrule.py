"""RRULE tests.

Two layers:

* hand-written cases for semantics and error handling;
* a randomised differential test against ``dateutil.rrule`` -- the de-facto
  reference implementation -- over thousands of generated rules.  This is the
  cheapest way to be confident that our "strict subset" really is the same
  subset everyone else means.
"""

from __future__ import annotations

import random
import time
from datetime import datetime
from itertools import islice

import pytest

pytest.importorskip("dateutil")

from dateutil.rrule import rrulestr  # noqa: E402

from wallclock.errors import SpecError  # noqa: E402
from wallclock.rrule import WEEKDAY_NAMES, parse_rrule  # noqa: E402

DT = datetime(2026, 1, 5, 9, 30)  # a Monday
END = datetime(2028, 1, 1)


def reference(rule: str, dtstart: datetime = DT, end: datetime = END) -> list[datetime]:
    out: list[datetime] = []
    deadline = time.monotonic() + 2.0
    for x in islice(rrulestr("RRULE:" + rule, dtstart=dtstart), 0, 500):
        if x >= end:
            break
        out.append(x)
        if time.monotonic() > deadline:
            raise TimeoutError(f"dateutil too slow on {rule}")
    return out


def test_count_and_interval() -> None:
    got = parse_rrule("FREQ=DAILY;INTERVAL=3;COUNT=4", DT).take(END)
    assert got == [datetime(2026, 1, 5, 9, 30), datetime(2026, 1, 8, 9, 30),
                   datetime(2026, 1, 11, 9, 30), datetime(2026, 1, 14, 9, 30)]


def test_monthly_skips_short_months() -> None:
    got = parse_rrule("FREQ=MONTHLY;BYMONTHDAY=31;COUNT=4", DT).take(END)
    assert [d.month for d in got] == [1, 3, 5, 7]


def test_feb_29_yearly_has_multi_year_gaps() -> None:
    r = parse_rrule("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29", DT)
    got = r.take(datetime(2040, 1, 1))
    assert [d.year for d in got] == [2028, 2032, 2036]


def test_until_is_inclusive_and_stops_expansion() -> None:
    got = parse_rrule("FREQ=DAILY;UNTIL=20260107T093000", DT).take(END)
    assert got[-1] == datetime(2026, 1, 7, 9, 30)


def test_bysetpos_last_weekday_of_month() -> None:
    got = parse_rrule("FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1;COUNT=3", DT).take(END)
    assert [d.date().isoformat() for d in got] == ["2026-01-30", "2026-02-27", "2026-03-31"]


def test_wkst_changes_interval_arithmetic() -> None:
    a = parse_rrule("FREQ=WEEKLY;INTERVAL=2;BYDAY=SU;WKST=MO;COUNT=2", DT).take(END)
    b = parse_rrule("FREQ=WEEKLY;INTERVAL=2;BYDAY=SU;WKST=SU;COUNT=2", DT).take(END)
    assert a[0] == datetime(2026, 1, 11, 9, 30)
    assert b[0] == datetime(2026, 1, 18, 9, 30)


def test_negative_monthday() -> None:
    got = parse_rrule("FREQ=MONTHLY;BYMONTHDAY=-1;COUNT=3", DT).take(END)
    assert [d.date().isoformat() for d in got] == ["2026-01-31", "2026-02-28", "2026-03-31"]


def test_rrule_prefix_and_case_are_accepted() -> None:
    a = parse_rrule("RRULE:freq=weekly;byday=mo;count=2", DT).take(END)
    b = parse_rrule("FREQ=WEEKLY;BYDAY=MO;COUNT=2", DT).take(END)
    assert a == b


def test_mixed_plain_and_ordinal_byday_is_a_union() -> None:
    # RFC 5545: BYDAY is a list, so plain and ordinal entries are unioned.
    # (dateutil returns nothing for this rule, and drops the plain entries when
    # an ordinal is present -- see the note in the randomised test.)
    got = parse_rrule("FREQ=MONTHLY;BYDAY=MO,-1FR;COUNT=4", DT).take(END)
    assert [d.date().isoformat() for d in got] == ["2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26"]
    got = parse_rrule("FREQ=MONTHLY;BYDAY=MO,-1FR", DT).take(datetime(2026, 2, 1))
    assert got[-1] == datetime(2026, 1, 30, 9, 30)  # the last Friday


def test_describe_is_human_readable() -> None:
    text = parse_rrule("FREQ=MONTHLY;BYDAY=1FR;BYHOUR=18;BYMINUTE=0", DT).describe()
    assert "month" in text and "1FR" in text and "18:00" in text


@pytest.mark.parametrize(
    "rule",
    [
        "",
        "INTERVAL=2",
        "FREQ=NOPE",
        "FREQ=SECONDLY",
        "FREQ=MINUTELY",
        "FREQ=DAILY;COUNT=0",
        "FREQ=DAILY;INTERVAL=0",
        "FREQ=DAILY;COUNT=2;COUNT=3",
        "FREQ=DAILY;BYMONTHDAY=0",
        "FREQ=DAILY;BYMONTHDAY=32",
        "FREQ=DAILY;BYMONTH=13",
        "FREQ=DAILY;BYHOUR=24",
        "FREQ=DAILY;BYSETPOS=1",
        "FREQ=WEEKLY;BYMONTHDAY=1",
        "FREQ=DAILY;BYDAY=1MO",
        "FREQ=WEEKLY;BYDAY=1MO",
        "FREQ=MONTHLY;BYDAY=9MO",
        "FREQ=YEARLY;BYDAY=60MO",
        "FREQ=YEARLY;BYWEEKNO=20",
        "FREQ=YEARLY;BYYEARDAY=100",
        "FREQ=DAILY;BYSECOND=0",
        "FREQ=DAILY;WKST=XX",
        "FREQ=DAILY;UNTIL=20260101T000000Z",
        "FREQ=DAILY;FOO=1",
        "FREQ=DAILY;BYDAY=MO;BYDAY=TU",
        "FREQ=DAILY;BYDAY=XX",
    ],
)
def test_invalid_rules_raise(rule: str) -> None:
    with pytest.raises(SpecError):
        parse_rrule(rule, DT)


def test_dtstart_is_required() -> None:
    with pytest.raises(SpecError):
        parse_rrule("FREQ=DAILY")


def test_aware_dtstart_is_stripped_to_wall_clock() -> None:
    from datetime import timezone

    aware = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)
    assert parse_rrule("FREQ=DAILY;COUNT=1", aware).dtstart == DT


def test_take_limit() -> None:
    assert len(parse_rrule("FREQ=DAILY", DT).take(END, 3)) == 3


# -- randomised differential testing --------------------------------------
def _random_rule(rng: random.Random) -> str:
    freq = rng.choice(["YEARLY", "MONTHLY", "WEEKLY", "DAILY", "HOURLY"])
    parts = [f"FREQ={freq}"]
    if rng.random() < 0.5:
        parts.append(f"INTERVAL={rng.randint(1, 4)}")
    if rng.random() < 0.35:
        parts.append(f"COUNT={rng.randint(1, 15)}")
    elif rng.random() < 0.25:
        parts.append("UNTIL=" + rng.choice(["20261231", "20270301T120000", "20270615"]))
    if rng.random() < 0.5:
        # dateutil silently drops plain weekdays when an ordinal is present in
        # the same BYDAY list, so the corpus uses one style or the other (the
        # RFC-correct union is pinned by test_mixed_plain_and_ordinal_byday).
        if freq in ("MONTHLY", "YEARLY") and rng.random() < 0.4:
            days = [
                f"{rng.choice([-2, -1, 1, 2])}{rng.choice(list(WEEKDAY_NAMES.values()))}"
                for _ in range(rng.randint(1, 2))
            ]
        else:
            days = rng.sample(list(WEEKDAY_NAMES.values()), rng.randint(1, 3))
        parts.append("BYDAY=" + ",".join(days))
    if freq != "WEEKLY" and rng.random() < 0.35:
        vals = rng.sample([1, 2, 15, 28, 29, 31, -1, -2], rng.randint(1, 2))
        parts.append("BYMONTHDAY=" + ",".join(str(v) for v in vals))
    if rng.random() < 0.25:
        parts.append("BYMONTH=" + ",".join(str(m) for m in rng.sample(range(1, 13), rng.randint(1, 3))))
    if rng.random() < 0.25:
        parts.append("BYHOUR=" + ",".join(str(h) for h in rng.sample(range(24), rng.randint(1, 3))))
    if rng.random() < 0.2:
        parts.append("BYMINUTE=" + ",".join(str(m) for m in rng.sample([0, 15, 30, 45], rng.randint(1, 3))))
    # BYSETPOS is covered by hand-written tests: with an unsynchronised DTSTART
    # the RFC leaves the period boundary undefined, and dateutil filters the
    # pre-DTSTART candidates before applying BYSETPOS while we apply the RFC
    # order (candidates, BYSETPOS, then DTSTART).  Comparing that corner would
    # test an undefined case, not our correctness.
    if freq == "WEEKLY" and rng.random() < 0.5:
        parts.append("WKST=" + rng.choice(list(WEEKDAY_NAMES.values())))
    return ";".join(parts)


def test_randomised_differential_against_dateutil() -> None:
    rng = random.Random(20261121)
    mismatches: list[str] = []
    checked = 0
    start = datetime(2026, 1, 5, 9, 30)
    end = datetime(2028, 1, 1)
    skipped_empty = 0
    while checked < 250:
        rule = _random_rule(rng)
        try:
            ours_obj = parse_rrule(rule, start)
        except SpecError as exc:
            # We are stricter than dateutil on a few RFC "MUST" points; the
            # corpus only generates rules we intend to accept.
            mismatches.append(f"{rule}: we raised {exc}")
            break
        ours = ours_obj.take(end, 500)
        if not ours:
            # dateutil never terminates on a rule that can never match, so
            # empty rules are not part of the differential corpus.
            skipped_empty += 1
            continue
        try:
            theirs = reference(rule, start, end)
        except Exception as exc:  # dateutil rejects some combinations; ours should too
            mismatches.append(f"{rule}: dateutil raised {exc!r} but we accepted")
            break
        if ours != theirs:
            idx = next((i for i, (a, b) in enumerate(zip(ours, theirs)) if a != b), min(len(ours), len(theirs)))
            mismatches.append(
                f"{rule}: differ at {idx} (ours={ours[idx:idx+1]} theirs={theirs[idx:idx+1]}, "
                f"lens {len(ours)}/{len(theirs)})"
            )
            break
        checked += 1
    assert not mismatches, mismatches[0]
    assert checked == 250
    assert skipped_empty < 60