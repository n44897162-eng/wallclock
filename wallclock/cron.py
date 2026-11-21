"""Vixie-style cron: parsing and wall-clock expansion.

The semantics implemented here are the ones actual cron daemons use, including
two details that bite people:

1. **Day-of-month / day-of-week are OR'd -- unless one field was written with a
   leading ``*``.**  ``0 0 1 * 1`` means "the 1st *or* any Monday", which is
   almost never what the author meant.  Vixie cron implements this by flagging a
   field as "starred" when its text starts with ``*`` (so ``*/2`` counts as
   starred) and switching from OR to AND.  We reproduce that exactly, because a
   verifier that disagrees with the daemon is worse than no verifier.

2. **Cron has no timezone of its own.**  These are wall-clock matchers; mapping
   them to instants is the job of :mod:`wallclock.schedule`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator, Mapping

from .errors import SpecError

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DOWS = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}
_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_FIELDS = (
    # name, low, high, names
    ("minute", 0, 59, None),
    ("hour", 0, 23, None),
    ("dom", 1, 31, None),
    ("month", 1, 12, _MONTHS),
    ("dow", 0, 7, _DOWS),
)


def _expand_names(text: str, names: Mapping[str, int] | None) -> str:
    if names is None:
        return text
    out = []
    token = ""
    for ch in text + ",":
        if ch.isalnum():
            token += ch
            continue
        if token:
            low = token.lower()
            if low in names:
                out.append(str(names[low]))
            elif low.isdigit():
                out.append(token)
            else:
                raise SpecError(f"unknown name {token!r}")
            token = ""
        out.append(ch)
    return "".join(out)[:-1]


def _parse_field(text: str, field: str, lo: int, hi: int, names: Mapping[str, int] | None) -> frozenset[int]:
    if not text:
        raise SpecError(f"empty {field} field")
    text = _expand_names(text.strip(), names)
    values: set[int] = set()
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise SpecError(f"empty list item in {field} field: {text!r}")
        step = 1
        if "/" in chunk:
            chunk, step_text = chunk.split("/", 1)
            try:
                step = int(step_text)
            except ValueError:
                raise SpecError(f"bad step {step_text!r} in {field} field") from None
            if step <= 0:
                raise SpecError(f"step must be positive in {field} field")
            if not chunk:
                raise SpecError(f"missing range before '/' in {field} field")
        if chunk == "*":
            start, end = lo, hi
        elif "-" in chunk:
            left, right = chunk.split("-", 1)
            start, end = _int(left, field), _int(right, field)
        else:
            start = end = _int(chunk, field)
        if start < lo or end > hi or start > end:
            raise SpecError(
                f"{field} field value {chunk!r} outside {lo}-{hi} (or reversed range)"
            )
        values.update(range(start, end + 1, step))
    if field == "dow":
        values = {0 if v == 7 else v for v in values}
    return frozenset(values)


def _int(text: str, field: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise SpecError(f"bad number {text!r} in {field} field") from None


def cron_dow(d: date) -> int:
    """Day of week with Sunday=0, the cron convention (Python: Monday=0)."""
    return (d.weekday() + 1) % 7


@dataclass(frozen=True)
class Cron:
    """A parsed 5-field cron expression (wall-clock, no timezone)."""

    expr: str
    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]
    dom_star: bool
    dow_star: bool

    # -- construction ------------------------------------------------------
    @classmethod
    def parse(cls, expr: str) -> "Cron":
        if not isinstance(expr, str) or not expr.strip():
            raise SpecError("empty cron expression")
        text = " ".join(expr.split())
        if text.startswith("@"):
            macro = _MACROS.get(text.lower())
            if macro is None:
                raise SpecError(f"unknown cron macro {text!r}")
            return cls.parse(macro)
        parts = text.split(" ")
        if len(parts) != 5:
            extra = ""
            if len(parts) == 6:
                extra = " (6-field cron with seconds is not supported)"
            raise SpecError(f"cron needs exactly 5 fields, got {len(parts)}{extra}: {expr!r}")
        for p in parts:
            if "?" in p:
                raise SpecError("'?' is not cron syntax; use '*'")
        parsed = {}
        star = {}
        for (name, lo, hi, names), part in zip(_FIELDS, parts):
            parsed[name] = _parse_field(part, name, lo, hi, names)
            star[name] = part.startswith("*")
        return cls(
            expr=text,
            minute=parsed["minute"],
            hour=parsed["hour"],
            dom=parsed["dom"],
            month=parsed["month"],
            dow=parsed["dow"],
            dom_star=star["dom"],
            dow_star=star["dow"],
        )

    # -- semantics ---------------------------------------------------------
    def day_matches(self, d: date) -> bool:
        if d.month not in self.month:
            return False
        dom_ok = d.day in self.dom
        dow_ok = cron_dow(d) in self.dow
        if self.dom_star or self.dow_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def times_of_day(self) -> list[tuple[int, int]]:
        return sorted((h, m) for h in sorted(self.hour) for m in sorted(self.minute))

    def iter_local(self, start: datetime, end: datetime) -> Iterator[datetime]:
        """Yield every matching naive wall-clock datetime in ``[start, end)``."""
        if end <= start:
            return
        times = self.times_of_day()
        if not times:
            return
        day = start.date()
        last = end.date()
        while day <= last:
            if self.day_matches(day):
                for hh, mm in times:
                    dt = datetime(day.year, day.month, day.day, hh, mm)
                    if start <= dt < end:
                        yield dt
            day += timedelta(days=1)

    def or_semantics(self) -> bool:
        """True when day matching is the surprising DOM-or-DOW form."""
        return not self.dom_star and not self.dow_star

    def only_one_of_dom_dow(self, d: date) -> bool:
        """True when ``d`` matches because of exactly one of DOM/DOW (OR mode)."""
        if not self.or_semantics():
            return False
        return (d.day in self.dom) != (cron_dow(d) in self.dow)

    # -- presentation ------------------------------------------------------
    def describe(self) -> str:
        minutes = " ".join(_rng(self.minute, 0))
        hours = " ".join(_rng(self.hour, 0))
        dom = " ".join(_rng(self.dom, 0))
        month = " ".join(_rng(self.month, 0))
        dow = " ".join(_rng(self.dow, 0))
        return f"cron {minutes} {hours} {dom} {month} {dow}".replace("  ", " ").strip()

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"Cron({self.expr!r})"


def _rng(values: Iterable[int], pad: int) -> list[str]:
    return [str(v).zfill(pad) if pad else str(v) for v in sorted(values)] if len(values) <= 8 else ["*"]


def parse_cron(expr: str) -> Cron:
    return Cron.parse(expr)