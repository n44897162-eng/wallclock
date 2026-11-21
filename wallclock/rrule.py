"""RFC 5545 RRULE: a strict subset, expanded on the naive wall-clock timeline.

The subset is chosen for scheduled jobs rather than calendar interop:

* ``FREQ`` in YEARLY / MONTHLY / WEEKLY / DAILY / HOURLY
* ``INTERVAL``, ``COUNT``, ``UNTIL`` (local wall clock)
* ``BYMONTH``, ``BYMONTHDAY`` (negative = from month end), ``BYDAY`` (with
  ordinals for MONTHLY/YEARLY), ``BYHOUR``, ``BYMINUTE``, ``BYSETPOS``, ``WKST``

Everything rejected is rejected loudly (``SpecError``) rather than silently
ignored, because "the rule did something other than what you wrote" is the
failure mode this project studies.  ``BYWEEKNO``/``BYYEARDAY``/``BYSECOND`` and
sub-hourly frequencies are out of scope on purpose: they multiply instances
without adding new *temporal* failure modes.

Expansion follows RFC 5545 section 3.3.10: candidates are built per period,
sorted, ``BYSETPOS`` is applied inside the period, then DTSTART/COUNT/UNTIL
bound the sequence.  ``tests/test_rrule.py`` cross-checks thousands of random
rules against ``dateutil.rrule`` (the de-facto reference implementation).
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterator, Sequence

from .errors import SpecError

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
WEEKDAY_NAMES = {v: k for k, v in WEEKDAYS.items()}
FREQS = ("YEARLY", "MONTHLY", "WEEKLY", "DAILY", "HOURLY")
_UNSUPPORTED = {
    "BYWEEKNO": "BYWEEKNO is not supported (use BYDAY with an ISO week tool externally)",
    "BYYEARDAY": "BYYEARDAY is not supported",
    "BYSECOND": "BYSECOND is not supported (schedules are minute-resolution)",
    "BYEASTER": "BYEASTER is not part of RFC 5545",
}
_BYDAY_RE = re.compile(r"^([+-]?\d{1,2})?([A-Z]{2})$")
_MAX_CANDIDATES = 200_000
_MAX_PERIODS = 400_000


@dataclass(frozen=True)
class RRule:
    """A parsed recurrence rule plus its DTSTART anchor (naive local time)."""

    raw: str
    dtstart: datetime
    freq: str
    interval: int = 1
    count: int | None = None
    until: datetime | None = None
    bymonth: tuple[int, ...] = ()
    bymonthday: tuple[int, ...] = ()
    byday: tuple[tuple[int | None, int], ...] = ()
    byhour: tuple[int, ...] = ()
    byminute: tuple[int, ...] = ()
    bysetpos: tuple[int, ...] = ()
    wkst: int = 0

    # -- parsing -----------------------------------------------------------
    @classmethod
    def parse(cls, text: str, dtstart: datetime | None = None) -> "RRule":
        if dtstart is None:
            raise SpecError("RRULE needs a DTSTART anchor")
        if not isinstance(dtstart, datetime):
            raise SpecError("DTSTART must be a datetime")
        if dtstart.tzinfo is not None:
            dtstart = dtstart.replace(tzinfo=None)
        dtstart = dtstart.replace(second=0, microsecond=0)

        raw = (text or "").strip()
        if not raw:
            raise SpecError("empty RRULE")
        if raw.upper().startswith("RRULE:"):
            raw = raw.split(":", 1)[1]
        parts: dict[str, str] = {}
        for chunk in raw.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                raise SpecError(f"bad RRULE part {chunk!r} (expected NAME=VALUE)")
            key, value = chunk.split("=", 1)
            key = key.strip().upper()
            if key in parts:
                raise SpecError(f"duplicate RRULE part {key}")
            parts[key] = value.strip().upper()
        if not parts:
            raise SpecError("empty RRULE")

        unknown = set(parts) - {
            "FREQ", "INTERVAL", "COUNT", "UNTIL", "BYMONTH", "BYMONTHDAY", "BYDAY",
            "BYHOUR", "BYMINUTE", "BYSETPOS", "WKST",
        }
        for key in unknown:
            if key in _UNSUPPORTED:
                raise SpecError(_UNSUPPORTED[key])
            raise SpecError(f"unknown RRULE part {key}")

        freq = parts.get("FREQ")
        if not freq:
            raise SpecError("RRULE is missing FREQ")
        if freq in ("SECONDLY", "MINUTELY"):
            raise SpecError(f"FREQ={freq} is out of scope (minute resolution)")
        if freq not in FREQS:
            raise SpecError(f"unknown FREQ={freq}")

        interval = _pos_int(parts.get("INTERVAL", "1"), "INTERVAL")
        count = _pos_int(parts["COUNT"], "COUNT") if "COUNT" in parts else None
        until = _parse_until(parts["UNTIL"]) if "UNTIL" in parts else None

        bymonth = _int_list(parts.get("BYMONTH"), "BYMONTH", 1, 12, allow_negative=False)
        bymonthday = _int_list(parts.get("BYMONTHDAY"), "BYMONTHDAY", 1, 31, allow_negative=True)
        byhour = _int_list(parts.get("BYHOUR"), "BYHOUR", 0, 23, allow_negative=False)
        byminute = _int_list(parts.get("BYMINUTE"), "BYMINUTE", 0, 59, allow_negative=False)
        bysetpos = _int_list(parts.get("BYSETPOS"), "BYSETPOS", 1, 366, allow_negative=True)
        byday = _parse_byday(parts.get("BYDAY"))

        wkst = 0
        if "WKST" in parts:
            code = parts["WKST"]
            if code not in WEEKDAYS:
                raise SpecError(f"bad WKST {code!r}")
            wkst = WEEKDAYS[code]

        if bymonthday and freq == "WEEKLY":
            raise SpecError("BYMONTHDAY must not be used with FREQ=WEEKLY")
        if byday and freq in ("DAILY", "WEEKLY") and any(o is not None for o, _ in byday):
            raise SpecError(f"ordinal BYDAY is only valid with FREQ=MONTHLY or YEARLY (got {freq})")
        if bysetpos and not (bymonth or bymonthday or byday or byhour or byminute):
            raise SpecError("BYSETPOS needs at least one other BY* part")
        if freq == "MONTHLY":
            for o, _ in byday:
                if o is not None and abs(o) > 5:
                    raise SpecError("ordinal BYDAY for FREQ=MONTHLY must be within +/-5")
        if freq == "YEARLY":
            for o, _ in byday:
                if o is not None and abs(o) > 53:
                    raise SpecError("ordinal BYDAY for FREQ=YEARLY must be within +/-53")

        return cls(
            raw=raw, dtstart=dtstart, freq=freq, interval=interval, count=count,
            until=until, bymonth=bymonth, bymonthday=bymonthday, byday=byday,
            byhour=byhour, byminute=byminute, bysetpos=bysetpos, wkst=wkst,
        )

    # -- expansion ---------------------------------------------------------
    def iter_local(self, end: datetime) -> Iterator[datetime]:
        """Yield recurrence ids (naive local datetimes) in ``[dtstart, end)``."""
        if end <= self.dtstart:
            return
        produced = 0
        for anchor in self._anchors(end):
            cands = self._candidates(anchor)
            if self.bysetpos:
                cands = _apply_setpos(cands, self.bysetpos)
            for dt in cands:
                if dt < self.dtstart:
                    continue
                if dt >= end:
                    return
                if self.until is not None and dt > self.until:
                    return
                yield dt
                produced += 1
                if self.count is not None and produced >= self.count:
                    return

    def last_local(self, bound: datetime) -> datetime | None:
        """Last recurrence id before ``bound`` for a finite rule (None if unbounded)."""
        if self.until is None and self.count is None:
            return None
        if self.count is not None and self.count > 100_000:
            return None
        last: datetime | None = None
        for dt in self.iter_local(bound):
            last = dt
        return last

    def take(self, end: datetime, limit: int = 1000) -> list[datetime]:
        out: list[datetime] = []
        for dt in self.iter_local(end):
            out.append(dt)
            if len(out) >= limit:
                break
        return out

    # -- internals ---------------------------------------------------------
    def _times(self) -> list[tuple[int, int]]:
        hours = sorted(self.byhour) if self.byhour else [self.dtstart.hour]
        minutes = sorted(self.byminute) if self.byminute else [self.dtstart.minute]
        return [(h, m) for h in hours for m in minutes]

    def _anchors(self, end: datetime) -> Iterator[object]:
        d0 = self.dtstart
        step = self.interval
        made = 0
        if self.freq == "YEARLY":
            year, cap = d0.year, end.year + 1
            while year <= cap:
                yield year
                year += step
                made += 1
                if made > _MAX_PERIODS:
                    raise SpecError("RRULE expands to too many periods; narrow the horizon")
        elif self.freq == "MONTHLY":
            y, m = d0.year, d0.month
            while (y, m) <= (end.year, end.month + 1):
                yield (y, m)
                m += step
                y += (m - 1) // 12
                m = (m - 1) % 12 + 1
                made += 1
                if made > _MAX_PERIODS:
                    raise SpecError("RRULE expands to too many periods; narrow the horizon")
        elif self.freq == "WEEKLY":
            d = _week_start(d0.date(), self.wkst)
            cap = end.date() + timedelta(days=7)
            while d <= cap:
                yield d
                d += timedelta(days=7 * step)
                made += 1
                if made > _MAX_PERIODS:
                    raise SpecError("RRULE expands to too many periods; narrow the horizon")
        elif self.freq == "DAILY":
            d = d0.date()
            cap = end.date() + timedelta(days=1)
            while d <= cap:
                yield d
                d += timedelta(days=step)
                made += 1
                if made > _MAX_PERIODS:
                    raise SpecError("RRULE expands to too many periods; narrow the horizon")
        else:  # HOURLY
            dt = d0.replace(minute=0)
            cap = end + timedelta(hours=1)
            while dt <= cap:
                yield dt
                dt += timedelta(hours=step)
                made += 1
                if made > _MAX_PERIODS:
                    raise SpecError("RRULE expands to too many periods; narrow the horizon")

    def _candidates(self, anchor: object) -> list[datetime]:
        dates: list[date] = []
        if self.freq == "YEARLY":
            year = int(anchor)  # type: ignore[arg-type]
            if self.bymonth:
                months = list(sorted(self.bymonth))
            elif self.bymonthday or self.byday:
                # RFC/dateutil expand the day filters over the whole year; the
                # DTSTART month is only the default when no day filter exists.
                months = list(range(1, 13))
            else:
                months = [self.dtstart.month]
            if self.bymonthday:
                for m in months:
                    for md in self.bymonthday:
                        d = _resolve_day(year, m, md)
                        if d is not None:
                            dates.append(d)
            elif self.byday:
                for m in months:
                    if self.bymonth:
                        dates.extend(_byday_in_month(year, m, self.byday))
                    else:
                        dates.extend(_byday_in_year(year, self.byday))
            else:
                for m in months:
                    d = _resolve_day(year, m, self.dtstart.day)
                    if d is not None:
                        dates.append(d)
            if self.bymonthday and self.byday:
                dates = [d for d in dates if self._byday_ok(d)]
            return self._dates_to_datetimes(dates)

        if self.freq == "MONTHLY":
            year, month = anchor  # type: ignore[misc]
            if self.bymonth and month not in self.bymonth:
                return []
            if self.bymonthday:
                for md in self.bymonthday:
                    d = _resolve_day(year, month, md)
                    if d is not None:
                        dates.append(d)
            elif self.byday:
                dates.extend(_byday_in_month(year, month, self.byday))
            else:
                d = _resolve_day(year, month, self.dtstart.day)
                if d is not None:
                    dates.append(d)
            if self.bymonthday and self.byday:
                dates = [d for d in dates if self._byday_ok(d)]
            return self._dates_to_datetimes(dates)

        if self.freq == "WEEKLY":
            week_start = anchor  # type: ignore[assignment]
            weekdays = [wd for _, wd in self.byday] if self.byday else [self.dtstart.weekday()]
            for wd in weekdays:
                d = week_start + timedelta(days=(wd - self.wkst) % 7)
                if self.bymonth and d.month not in self.bymonth:
                    continue
                dates.append(d)
            return self._dates_to_datetimes(dates)

        if self.freq == "DAILY":
            d = anchor  # type: ignore[assignment]
            if not self._day_passes(d):
                return []
            return self._dates_to_datetimes([d])

        # HOURLY
        dt = anchor  # type: ignore[assignment]
        d = dt.date()
        if self.byhour and dt.hour not in self.byhour:
            return []
        if not self._day_passes(d):
            return []
        minutes = sorted(self.byminute) if self.byminute else [self.dtstart.minute]
        return [datetime(d.year, d.month, d.day, dt.hour, mi) for mi in minutes]

    def _day_passes(self, d: date) -> bool:
        if self.bymonth and d.month not in self.bymonth:
            return False
        if self.bymonthday and not any(_day_matches(d, md) for md in self.bymonthday):
            return False
        if self.byday and not self._byday_ok(d):
            return False
        return True

    def _byday_ok(self, d: date) -> bool:
        """True if ``d`` satisfies this rule's BYDAY (plain or ordinal)."""
        scope_month = not (self.freq == "YEARLY" and not self.bymonth)
        for ordinal, wd in self.byday:
            if d.weekday() != wd:
                continue
            if ordinal is None:
                return True
            if scope_month:
                forward = (d.day - 1) // 7 + 1
                backward = -((_month_len(d.year, d.month) - d.day) // 7 + 1)
            else:
                doy = d.timetuple().tm_yday
                forward = (doy - 1) // 7 + 1
                backward = -((365 + calendar.isleap(d.year) - doy) // 7 + 1)
            if ordinal in (forward, backward):
                return True
        return False

    def _dates_to_datetimes(self, dates: Sequence[date]) -> list[datetime]:
        times = self._times()
        out = {datetime(d.year, d.month, d.day, h, m) for d in dates for h, m in times}
        if len(out) > _MAX_CANDIDATES:
            raise SpecError("RRULE period generates too many candidates; simplify the rule")
        return sorted(out)

    def anchor_warning(self) -> str | None:
        """Warn when DTSTART itself does not satisfy the rule's filters.

        RFC 5545 calls the result of an unsynchronised DTSTART "undefined", and
        real implementations disagree about it -- exactly the kind of ambiguity
        a verifier should surface instead of silently resolving.
        """
        issues: list[str] = []
        start = self.dtstart.date()
        if self.bymonth and self.dtstart.month not in self.bymonth:
            issues.append("BYMONTH")
        if self.bymonthday and not any(_day_matches(start, md) for md in self.bymonthday):
            issues.append("BYMONTHDAY")
        if self.byday and not self._byday_ok(start):
            issues.append("BYDAY")
        if self.byhour and self.dtstart.hour not in self.byhour:
            issues.append("BYHOUR")
        if self.byminute and self.dtstart.minute not in self.byminute:
            issues.append("BYMINUTE")
        if not issues:
            return None
        return (
            "DTSTART does not satisfy " + ", ".join(issues) +
            "; RFC 5545 leaves the first period undefined"
        )

    # -- presentation ------------------------------------------------------
    def describe(self) -> str:
        bits: list[str] = []
        unit = {"YEARLY": "year", "MONTHLY": "month", "WEEKLY": "week", "DAILY": "day", "HOURLY": "hour"}[self.freq]
        if self.interval == 1:
            bits.append(f"every {unit}")
        else:
            bits.append(f"every {self.interval} {unit}s")
        if self.byday:
            bits.append("on " + ", ".join(
                (f"{o}{WEEKDAY_NAMES[wd]}" if o is not None else WEEKDAY_NAMES[wd]) for o, wd in self.byday
            ))
        if self.bymonthday:
            bits.append("on day " + ",".join(str(v) for v in self.bymonthday))
        if self.bymonth:
            bits.append("in " + ",".join(calendar.month_abbr[m] for m in self.bymonth))
        if self.byhour or self.byminute:
            bits.append("at " + ",".join(f"{h:02d}:{m:02d}" for h, m in self._times()))
        else:
            bits.append(f"at {self.dtstart.hour:02d}:{self.dtstart.minute:02d}")
        if self.count is not None:
            bits.append(f"x{self.count}")
        if self.until is not None:
            bits.append(f"until {self.until.isoformat()}")
        return " ".join(bits)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _pos_int(text: str, name: str) -> int:
    try:
        v = int(text)
    except ValueError:
        raise SpecError(f"{name} must be an integer, got {text!r}") from None
    if v < 1:
        raise SpecError(f"{name} must be >= 1, got {v}")
    return v


def _int_list(
    text: str | None, name: str, lo: int, hi: int, allow_negative: bool
) -> tuple[int, ...]:
    if text is None:
        return ()
    out: list[int] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise SpecError(f"empty item in {name}")
        try:
            v = int(chunk)
        except ValueError:
            raise SpecError(f"bad integer {chunk!r} in {name}") from None
        if v == 0 and name == "BYSETPOS":
            raise SpecError("BYSETPOS must not be 0")
        if allow_negative:
            if not (lo <= abs(v) <= hi):
                raise SpecError(f"{name} value {v} outside +/-{lo}..{hi}")
        else:
            if not (lo <= v <= hi):
                raise SpecError(f"{name} value {v} outside {lo}..{hi}")
        out.append(v)
    return tuple(sorted(set(out)))


def _parse_byday(text: str | None) -> tuple[tuple[int | None, int], ...]:
    if text is None:
        return ()
    out: list[tuple[int | None, int]] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        m = _BYDAY_RE.match(chunk)
        if not m:
            raise SpecError(f"bad BYDAY item {chunk!r}")
        ord_text, code = m.groups()
        if code not in WEEKDAYS:
            raise SpecError(f"bad weekday {code!r} in BYDAY")
        if ord_text is None:
            out.append((None, WEEKDAYS[code]))
        else:
            n = int(ord_text)
            if n == 0:
                raise SpecError("BYDAY ordinal must not be 0")
            out.append((n, WEEKDAYS[code]))
    return tuple(out)


def _parse_until(text: str) -> datetime:
    raw = text.strip().upper()
    if raw.endswith("Z"):
        raise SpecError(
            "UNTIL with 'Z' must be converted by the schedule layer (RRULE itself is wall-clock)"
        )
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise SpecError(f"bad UNTIL {text!r}") from None


def _week_start(d: date, wkst: int) -> date:
    return d - timedelta(days=(d.weekday() - wkst) % 7)


def _month_len(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _day_matches(d: date, monthday: int) -> bool:
    if monthday > 0:
        return d.day == monthday
    return d.day == _month_len(d.year, d.month) + monthday + 1


def _resolve_day(year: int, month: int, monthday: int) -> date | None:
    last = _month_len(year, month)
    day = monthday if monthday > 0 else last + monthday + 1
    if 1 <= day <= last:
        return date(year, month, day)
    return None


def _byday_in_month(year: int, month: int, byday: Sequence[tuple[int | None, int]]) -> list[date]:
    out: list[date] = []
    last = _month_len(year, month)
    for ordinal, wd in byday:
        if ordinal is None:
            d = date(year, month, 1)
            d += timedelta(days=(wd - d.weekday()) % 7)
            while d.month == month:
                out.append(d)
                d += timedelta(days=7)
        elif ordinal > 0:
            d = date(year, month, 1)
            d += timedelta(days=(wd - d.weekday()) % 7 + 7 * (ordinal - 1))
            if d.month == month:
                out.append(d)
        else:
            d = date(year, month, last)
            d -= timedelta(days=(d.weekday() - wd) % 7 + 7 * (-ordinal - 1))
            if d.month == month:
                out.append(d)
    return sorted(set(out))


def _byday_in_year(year: int, byday: Sequence[tuple[int | None, int]]) -> list[date]:
    out: list[date] = []
    for ordinal, wd in byday:
        if ordinal is None:
            d = date(year, 1, 1)
            d += timedelta(days=(wd - d.weekday()) % 7)
            while d.year == year:
                out.append(d)
                d += timedelta(days=7)
        elif ordinal > 0:
            d = date(year, 1, 1)
            d += timedelta(days=(wd - d.weekday()) % 7 + 7 * (ordinal - 1))
            if d.year == year:
                out.append(d)
        else:
            d = date(year, 12, 31)
            d -= timedelta(days=(d.weekday() - wd) % 7 + 7 * (-ordinal - 1))
            if d.year == year:
                out.append(d)
    return sorted(set(out))


def _apply_setpos(cands: Sequence[datetime], setpos: Sequence[int]) -> list[datetime]:
    n = len(cands)
    picked: set[datetime] = set()
    for p in setpos:
        idx = p - 1 if p > 0 else n + p
        if 0 <= idx < n:
            picked.add(cands[idx])
    return sorted(picked)


def parse_rrule(text: str, dtstart: datetime | None = None) -> RRule:
    return RRule.parse(text, dtstart)