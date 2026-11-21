"""Cron parser/expander tests, including the DOM/DOW OR trap."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from wallclock.cron import Cron, cron_dow, parse_cron
from wallclock.errors import SpecError


def test_simple_daily() -> None:
    c = parse_cron("30 2 * * *")
    assert c.minute == {30} and c.hour == {2}
    assert c.day_matches(date(2026, 6, 1))
    assert c.times_of_day() == [(2, 30)]


def test_steps_ranges_lists() -> None:
    c = parse_cron("*/15 9-17/4 1,15 * *")
    assert c.minute == {0, 15, 30, 45}
    assert c.hour == {9, 13, 17}
    assert c.dom == {1, 15}
    assert c.dow == set(range(7))


def test_month_and_dow_names_are_case_insensitive() -> None:
    c = parse_cron("0 0 * jan,MAr mon-Fri")
    assert c.month == {1, 3}
    assert c.dow == {1, 2, 3, 4, 5}


def test_sunday_may_be_7() -> None:
    assert parse_cron("0 0 * * 7").dow == {0}
    assert parse_cron("0 0 * * 0").dow == {0}
    assert parse_cron("0 0 * * 5-7").dow == {5, 6, 0}


def test_macros() -> None:
    assert parse_cron("@daily") == parse_cron("0 0 * * *")
    assert parse_cron("@hourly") == parse_cron("0 * * * *")
    assert parse_cron("@weekly") == parse_cron("0 0 * * 0")
    assert parse_cron("@yearly") == parse_cron("0 0 1 1 *")


def test_dom_dow_are_ored_when_neither_is_starred() -> None:
    c = parse_cron("0 0 1 * 1")  # "the 1st or any Monday" -- a classic bug
    assert c.or_semantics()
    assert c.day_matches(date(2026, 6, 1))  # Monday AND the 1st
    assert c.day_matches(date(2026, 6, 8))  # Monday only
    assert c.day_matches(date(2026, 7, 1))  # the 1st only
    assert not c.day_matches(date(2026, 6, 2))  # plain Tuesday
    assert c.only_one_of_dom_dow(date(2026, 6, 8))
    assert not c.only_one_of_dom_dow(date(2026, 6, 1))


def test_a_starred_field_flips_or_to_and() -> None:
    # Vixie flags a field as "star" when its text starts with '*', so */2 counts.
    c = parse_cron("0 0 */2 * 1")
    assert c.dom_star and not c.dow_star
    assert not c.or_semantics()
    assert c.day_matches(date(2026, 6, 1))  # odd day + Monday
    assert not c.day_matches(date(2026, 6, 8))  # Monday but even
    assert not c.day_matches(date(2026, 6, 3))  # odd but Wednesday


def test_star_dom_restricts_to_dow_only() -> None:
    c = parse_cron("0 0 * * 1")
    assert c.day_matches(date(2026, 6, 1))
    assert not c.day_matches(date(2026, 6, 2))


def test_star_dow_restricts_to_dom_only() -> None:
    c = parse_cron("0 0 15 * *")
    assert c.day_matches(date(2026, 6, 15))
    assert not c.day_matches(date(2026, 6, 16))


def test_month_filter_is_always_anded() -> None:
    c = parse_cron("0 0 1 2 *")
    assert c.day_matches(date(2026, 2, 1))
    assert not c.day_matches(date(2026, 3, 1))


def test_iter_local_is_half_open_and_sorted() -> None:
    c = parse_cron("0 12 * * *")
    got = list(c.iter_local(datetime(2026, 1, 1), datetime(2026, 1, 4)))
    assert got == [datetime(2026, 1, 1, 12), datetime(2026, 1, 2, 12), datetime(2026, 1, 3, 12)]


def test_iter_local_handles_empty_window() -> None:
    assert list(parse_cron("* * * * *").iter_local(datetime(2026, 1, 1), datetime(2026, 1, 1))) == []


def test_cron_dow_matches_python() -> None:
    assert cron_dow(date(2026, 6, 1)) == 1  # Monday
    assert cron_dow(date(2026, 6, 7)) == 0  # Sunday


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "   ",
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * 32 * *",
        "* * * 13 *",
        "* * * * 8",
        "*/0 * * * *",
        "1-0 * * * *",
        "10-5 * * * *",
        "? * * * *",
        "@nope",
        "x * * * *",
        "1,,2 * * * *",
        "1/ * * * *",
    ],
)
def test_bad_expressions_raise(expr: str) -> None:
    with pytest.raises(SpecError):
        parse_cron(expr)


def test_equality_and_str() -> None:
    assert Cron.parse("0  0  *  * *") == Cron.parse("@daily")
    assert Cron.parse("0 0 * * *").expr == "0 0 * * *"