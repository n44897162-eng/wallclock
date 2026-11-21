"""Repair synthesis: search for the smallest schedule change that satisfies the contracts.

This is not a solver.  It is a bounded, honest search over the edits that
actually fix schedule-vs-timezone problems in practice:

1. **policy** -- keep the rule, change what happens in a gap/overlap
   (``skip`` -> ``clamp_end``, ``earlier`` -> ``later`` ...);
2. **local time shift** -- move the job by 15m/30m/1h/2h/3h;
3. **add a run** -- a second daily firing 12h away, so one lost occurrence
   cannot create a two-day hole;
4. **UTC anchoring** -- stop following the wall clock entirely.

Every candidate is re-verified by the same exhaustive checker that judged the
original.  A repair that has not been re-verified is not offered: the whole
point is to replace "this should work" with "this does work over the window".
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from .cron import Cron, parse_cron
from .errors import SpecError
from .rrule import parse_rrule
from .schedule import Schedule
from .tz import Policy
from .verify import Contract, Report, parse_contract, simulate, verify


@dataclass
class Repair:
    description: str
    cost: int
    schedule: Schedule
    report: Report

    @property
    def ok(self) -> bool:
        return self.report.ok

    @property
    def errors(self) -> int:
        return len(self.report.errors)


def repair(
    schedule: Schedule,
    contracts: Sequence[Contract] | Sequence[dict] = (),
    lo: datetime | None = None,
    hi: datetime | None = None,
    max_candidates: int = 16,
) -> list[Repair]:
    """Return verified candidate fixes, best first (passing, then cheapest)."""
    if lo is None or hi is None:
        raise SpecError("repair() needs a window (lo, hi)")
    parsed = [c if isinstance(c, Contract) else parse_contract(c) for c in contracts]
    firings = simulate(schedule, lo, hi).firings

    seen: set[str] = set()
    out: list[Repair] = []
    for description, cost, candidate in _candidates(schedule, firings):
        if candidate is None:
            continue
        key = repr(sorted(candidate.to_dict().items()))
        if key in seen:
            continue
        seen.add(key)
        report = verify(candidate, parsed, lo, hi)
        out.append(Repair(description, cost, candidate, report))
        if len(out) >= max_candidates:
            break
    out.sort(key=lambda r: (not r.ok, r.errors, r.cost, r.description))
    return out


def _candidates(
    schedule: Schedule, firings: Sequence
) -> Iterable[tuple[str, int, Schedule | None]]:
    # 1. resolution policies
    gap_text = {
        "clamp_end": "gap=clamp_end: if the wall-clock time is missing, run at the first valid instant",
        "shift_forward": "gap=shift_forward: if the time is missing, shift it forward by the gap",
        "shift_backward": "gap=shift_backward: if the time is missing, run just before the gap",
    }
    for gap, text in gap_text.items():
        if gap != schedule.policy.gap:
            yield (
                text,
                1,
                schedule.with_policy(Policy(gap=gap, ambiguous=schedule.policy.ambiguous)),
            )
    amb_text = {
        "earlier": "ambiguous=earlier: run once, on the first of the two repeated instants",
        "later": "ambiguous=later: run once, on the second of the two repeated instants",
        "both": "ambiguous=both: run on both repeated instants",
    }
    for amb, text in amb_text.items():
        if amb != schedule.policy.ambiguous:
            yield (
                text,
                1,
                schedule.with_policy(Policy(gap=schedule.policy.gap, ambiguous=amb)),
            )

    # 2. local time shifts
    for delta, cost in (
        (timedelta(minutes=15), 1), (timedelta(minutes=-15), 1),
        (timedelta(minutes=30), 1), (timedelta(minutes=-30), 1),
        (timedelta(hours=1), 1), (timedelta(hours=-1), 1),
        (timedelta(hours=2), 2), (timedelta(hours=-2), 2),
        (timedelta(hours=3), 2), (timedelta(hours=-3), 2),
    ):
        shifted = _shift_local(schedule, delta)
        if shifted is not None:
            yield (f"move the job by {_signed(delta)} local time", cost, shifted)

    # 3. add a second daily run, 12h away
    extra = _add_second_run(schedule)
    if extra is not None:
        yield ("add a second daily run 12h away (halves the worst-case hole)", 2, extra)

    # 4. anchor in UTC
    utc_anchor = _anchor_in_utc(schedule, firings)
    if utc_anchor is not None:
        yield (
            "anchor the recurrence in UTC (no DST following; local time drifts by the offset change)",
            3,
            utc_anchor,
        )


def _shift_local(schedule: Schedule, delta: timedelta) -> Schedule | None:
    if schedule.kind == "cron":
        assert schedule.cron is not None
        expr = _shift_cron(schedule.cron, delta)
        return schedule.with_cron(expr) if expr else None
    rule = schedule.rrule
    if rule is None:
        return None
    if rule.byhour or rule.byminute:
        return None  # time is expanded by BY* parts; shifting DTSTART would not move it
    new_start = rule.dtstart + delta
    return schedule.with_rrule(rule.raw, new_start)


def _shift_cron(cron: Cron, delta: timedelta) -> str | None:
    total_minutes = int(delta.total_seconds()) // 60
    if total_minutes * 60 != int(delta.total_seconds()):
        return None
    dm = total_minutes
    minutes = sorted(cron.minute)
    carries = {(m + dm) // 60 for m in minutes}
    if len(carries) != 1:
        return None
    carry = carries.pop()
    new_minutes = {(m + dm) % 60 for m in minutes}
    hours = sorted(cron.hour)
    if hours == list(range(24)):
        new_hours = set(hours)
    else:
        hour_carries = {(h + carry) // 24 for h in hours}
        if len(hour_carries) != 1 or hour_carries.pop() != 0:
            return None  # would move the job to yesterday/tomorrow; not modelled
        new_hours = {(h + carry) % 24 for h in hours}
    parts = cron.expr.split()
    parts[0] = _render(new_minutes, 60)
    parts[1] = _render(new_hours, 24)
    return " ".join(parts)


def _add_second_run(schedule: Schedule) -> Schedule | None:
    if schedule.kind != "cron" or schedule.cron is None:
        return None
    cron = schedule.cron
    if len(cron.hour) != 1 or len(cron.minute) != 1:
        return None
    h = next(iter(cron.hour))
    parts = cron.expr.split()
    parts[1] = _render({h, (h + 12) % 24}, 24)
    return schedule.with_cron(" ".join(parts))


def _anchor_in_utc(schedule: Schedule, firings: Sequence) -> Schedule | None:
    if not firings:
        return None
    if schedule.kind == "cron":
        cron = schedule.cron
        assert cron is not None
        if len(cron.hour) != 1 or len(cron.minute) != 1:
            return None
    else:
        rule = schedule.rrule
        if rule is None or rule.byhour or rule.byminute:
            return None
    # the UTC time-of-day the job spends most of its life at
    seen = Counter((f.utc.hour, f.utc.minute) for f in firings)
    (h, m), _ = seen.most_common(1)[0]
    if schedule.kind == "cron":
        parts = schedule.cron.expr.split()  # type: ignore[union-attr]
        parts[0] = str(m)
        parts[1] = str(h)
        base = schedule.with_cron(" ".join(parts))
        return base.with_anchor("utc")
    rule = schedule.rrule
    assert rule is not None
    start = rule.dtstart.replace(hour=h, minute=m)
    return schedule.with_rrule(rule.raw, start).with_anchor("utc")


def _render(values: Iterable[int], modulus: int) -> str:
    vals = sorted(values)
    if len(vals) == modulus:
        return "*"
    if len(vals) == 1:
        return str(vals[0])
    if vals == list(range(vals[0], vals[-1] + 1)):
        return f"{vals[0]}-{vals[-1]}"
    return ",".join(str(v) for v in vals)


def _signed(delta: timedelta) -> str:
    from .verify import format_duration

    text = format_duration(abs(delta))
    return ("+" if delta >= timedelta(0) else "-") + text