"""Exact timezone primitives: transitions and local -> UTC resolution.

Why this module exists
----------------------
A recurring job is almost always specified in *wall-clock* terms: "02:30 every
day".  Turning that sentence into real instants is a partial function.  Around
a DST transition a local datetime is one of:

* **unique**      -- exactly one instant exists;
* **ambiguous**   -- two instants exist (clocks fall back, the hour repeats);
* **nonexistent** -- zero instants exist (clocks spring forward, the hour is
  skipped).

CPython's :mod:`zoneinfo` implements :pep:`495` ``fold`` but *silently* picks a
value for nonexistent times -- ``datetime(2026, 3, 29, 2, 30,
tzinfo=ZoneInfo("Europe/Berlin"))`` quietly becomes 03:30 local.  Silent
behaviour is exactly what produces "my job did not run last night" pages, so
this module never guesses: every resolution is explicit, tagged with the policy
that produced it, and accompanied by the offset used.  Witnesses in the report
are built from these tags.

The transition scanner
----------------------
``zoneinfo`` does not expose the tzdata transition list.  We sample the UTC
offset on a grid and binary-search (to the second) every change.  Sampling is
the only approximation in this file: two transitions that cancel out inside one
grid step would be missed, which is why the default step is one hour (tzdata has
no such pair; a test pins the known count for several zones).
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import SpecError, ZoneError

UTC = timezone.utc
SECOND = timedelta(seconds=1)

#: policies for a local time that does not exist (spring forward)
GAP_POLICIES = ("skip", "shift_forward", "shift_backward", "clamp_end")
#: policies for a local time that exists twice (fall back)
AMBIGUOUS_POLICIES = ("earlier", "later", "both")

_TZDATA_CANDIDATES = (
    "/usr/share/zoneinfo/tzdata.zi",
    "/usr/lib/zoneinfo/tzdata.zi",
    "/usr/share/lib/zoneinfo/tzdata.zi",
)


@dataclass(frozen=True)
class Transition:
    """A change of UTC offset.

    ``at`` is the first UTC instant that uses the new offset.  For a spring
    forward ``gap`` is positive and describes how much wall-clock time is
    skipped; for a fall back it is negative and ``|gap|`` is repeated.
    """

    at: datetime
    before: timedelta
    after: timedelta

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("Transition.at must be timezone-aware (UTC)")

    @property
    def gap(self) -> timedelta:
        return self.after - self.before

    @property
    def is_gap(self) -> bool:
        return self.after > self.before

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        arrow = f"{_fmt_off(self.before)} -> {_fmt_off(self.after)}"
        return f"{self.at.isoformat()} {arrow}"


@dataclass(frozen=True)
class Resolution:
    """One concrete instant obtained from one wall-clock datetime.

    ``kind`` is one of ``normal``, ``ambiguous_earlier``, ``ambiguous_later``,
    ``gap_forward``, ``gap_backward``, ``gap_clamp_end``.  A skipped local time
    yields no :class:`Resolution` at all -- the caller decides what that means
    (usually: the occurrence is lost).
    """

    local: datetime  # naive wall clock, as written by the user
    utc: datetime  # aware, exactly what the scheduler would fire
    kind: str
    offset: timedelta

    @property
    def exists(self) -> bool:
        return self.kind == "normal"

    @property
    def was_shifted(self) -> bool:
        return self.kind.startswith("gap_")

    @property
    def is_ambiguous(self) -> bool:
        return self.kind.startswith("ambiguous_")


@dataclass(frozen=True)
class Policy:
    """How to resolve the two impossible cases.

    Defaults mirror what most schedulers do *silently*: skip nonexistent local
    times, fire once for ambiguous ones.  Making it a first-class object is what
    lets ``wallclock`` say "if you only change the policy, this contract holds".
    """

    gap: str = "skip"
    ambiguous: str = "earlier"

    def __post_init__(self) -> None:
        if self.gap not in GAP_POLICIES:
            raise SpecError(f"unknown gap policy {self.gap!r}; choose from {', '.join(GAP_POLICIES)}")
        if self.ambiguous not in AMBIGUOUS_POLICIES:
            raise SpecError(
                f"unknown ambiguous policy {self.ambiguous!r}; choose from {', '.join(AMBIGUOUS_POLICIES)}"
            )

    def describe(self) -> str:
        return f"gap={self.gap}, ambiguous={self.ambiguous}"


DEFAULT_POLICY = Policy()


class Zone:
    """A named IANA zone plus the exact machinery wallclock needs."""

    __slots__ = ("name", "tz")

    def __init__(self, name: str) -> None:
        if not isinstance(name, str) or not name:
            raise ZoneError("timezone name must be a non-empty string")
        try:
            self.tz = ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - depends on host tzdata
            raise ZoneError(f"unknown timezone {name!r} (is the tzdata package installed?)") from exc
        except ValueError as exc:
            raise ZoneError(f"invalid timezone {name!r}: {exc}") from exc
        self.name = name

    # -- basic conversions -------------------------------------------------
    def offset_at(self, utc_dt: datetime) -> timedelta:
        """UTC offset in force at an aware instant."""
        return utc_dt.astimezone(self.tz).utcoffset()  # type: ignore[return-value]

    def wall(self, utc_dt: datetime) -> datetime:
        """Naive wall-clock reading of an instant in this zone."""
        return utc_dt.astimezone(self.tz).replace(tzinfo=None)

    def wall_of(self, utc_dt: datetime) -> datetime:
        return self.wall(utc_dt)

    def today(self) -> date:
        return datetime.now(UTC).astimezone(self.tz).date()

    # -- transitions -------------------------------------------------------
    def transitions(
        self,
        lo: datetime,
        hi: datetime,
        step: timedelta = timedelta(hours=1),
    ) -> list[Transition]:
        """All offset changes in ``[lo, hi)``, found to the second."""
        if lo.tzinfo is None or hi.tzinfo is None:
            raise SpecError("transitions() needs timezone-aware bounds")
        if hi <= lo:
            raise SpecError("transitions() needs lo < hi")
        if step < timedelta(seconds=1):
            raise SpecError("transitions() step must be >= 1 second")
        return _scan(self.name, lo, hi, int(step.total_seconds()))

    def transition_near(
        self, instant: datetime, lookback: timedelta = timedelta(days=400)
    ) -> Transition | None:
        """The most recent transition at or before ``instant`` within ``lookback``."""
        hits = self.transitions(instant - lookback, instant + SECOND)
        return hits[-1] if hits else None

    # -- resolution --------------------------------------------------------
    def candidates(self, local: datetime) -> list[datetime]:
        """Distinct instants whose wall clock reads ``local`` (0, 1 or 2)."""
        local = local.replace(tzinfo=None)
        out: list[datetime] = []
        for fold in (0, 1):
            probe = local.replace(tzinfo=self.tz, fold=fold).astimezone(UTC)
            if probe in out:
                continue
            if self.wall(probe) == local:
                out.append(probe)
        out.sort()
        return out

    def resolve(self, local: datetime, policy: Policy = DEFAULT_POLICY) -> list[Resolution]:
        """Map one wall-clock datetime to zero, one or two instants.

        The returned list is sorted by instant.  An empty list means the local
        time does not exist and the policy is ``skip``.
        """
        local = local.replace(tzinfo=None)
        d0 = local.replace(tzinfo=self.tz, fold=0)
        d1 = local.replace(tzinfo=self.tz, fold=1)
        off0, off1 = d0.utcoffset(), d1.utcoffset()
        assert off0 is not None and off1 is not None

        valid = self.candidates(local)
        if len(valid) == 2:
            if policy.ambiguous == "both":
                return [
                    Resolution(local, valid[0], "ambiguous_earlier", self.offset_at(valid[0])),
                    Resolution(local, valid[1], "ambiguous_later", self.offset_at(valid[1])),
                ]
            pick, kind = (valid[0], "ambiguous_earlier") if policy.ambiguous == "earlier" else (
                valid[1],
                "ambiguous_later",
            )
            return [Resolution(local, pick, kind, self.offset_at(pick))]
        if len(valid) == 1:
            return [Resolution(local, valid[0], "normal", self.offset_at(valid[0]))]

        # Nonexistent: PEP 495 fold=0 carries the pre-transition offset.
        if policy.gap == "skip":
            return []
        if policy.gap == "shift_forward":
            u, kind = local.replace(tzinfo=self.tz, fold=0).astimezone(UTC), "gap_forward"
            return [Resolution(local, u, kind, self.offset_at(u))]
        if policy.gap == "shift_backward":
            u, kind = local.replace(tzinfo=self.tz, fold=1).astimezone(UTC), "gap_backward"
            return [Resolution(local, u, kind, self.offset_at(u))]
        # clamp_end: fire at the first instant the clock is valid again.
        u_back = local.replace(tzinfo=self.tz, fold=1).astimezone(UTC)
        u_fwd = local.replace(tzinfo=self.tz, fold=0).astimezone(UTC)
        if u_fwd <= u_back:  # pragma: no cover - defensive
            raise ZoneError(f"inconsistent gap for {local} in {self.name}")
        edge = _first_instant_with_offset(self, u_back, u_fwd, off1)
        return [Resolution(local, edge, "gap_clamp_end", self.offset_at(edge))]

    def exists(self, local: datetime) -> bool:
        return bool(self.candidates(local))

    def fire_times(self, local: datetime, policy: Policy = DEFAULT_POLICY) -> list[datetime]:
        """Convenience: instants for a local datetime under a policy."""
        return [r.utc for r in self.resolve(local, policy)]

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Zone({self.name!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Zone) and other.name == self.name

    def __hash__(self) -> int:
        return hash(("wallclock.Zone", self.name))


def get_zone(name: str) -> Zone:
    """Cached constructor; zone objects are immutable and cheap to share."""
    return _zone_cache(name)


@functools.lru_cache(maxsize=256)
def _zone_cache(name: str) -> Zone:
    return Zone(name)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=512)
def _scan(name: str, lo: datetime, hi: datetime, step_seconds: int) -> list[Transition]:
    zone = ZoneInfo(name)
    step = timedelta(seconds=step_seconds)

    def offset(t: datetime) -> timedelta:
        return t.astimezone(zone).utcoffset()  # type: ignore[return-value]

    out: list[Transition] = []
    t = lo
    cur = offset(t)
    while t < hi:
        nxt = min(t + step, hi)
        nxt_off = offset(nxt)
        if nxt_off != cur:
            a, b = t, nxt
            # invariant: offset(a) == cur, offset(b) != cur
            while (b - a) > SECOND:
                mid = a + (b - a) // 2
                if offset(mid) == cur:
                    a = mid
                else:
                    b = mid
            out.append(Transition(b, cur, offset(b)))
            cur = offset(b)
        t = nxt
        if t - lo > timedelta(days=366 * 40):  # absolute safety valve
            break
    return out


def _first_instant_with_offset(zone: Zone, lo: datetime, hi: datetime, want: timedelta) -> datetime:
    """First instant in ``(lo, hi]`` whose offset is ``want`` (binary search)."""
    a, b = lo, hi
    while (b - a) > SECOND:
        mid = a + (b - a) // 2
        if zone.offset_at(mid) == want:
            b = mid
        else:
            a = mid
    return b


def tzdata_version() -> str | None:
    """Version string of the host tzdata, for reproducibility footers."""
    for path in _TZDATA_CANDIDATES:
        p = Path(path)
        if not p.exists():
            continue
        try:
            head = p.read_text(errors="replace")[:400]
        except OSError:  # pragma: no cover - unreadable system file
            continue
        m = re.search(r"version\s+([0-9]{4}[a-z]+)", head, re.I)
        if m:
            return m.group(1)
    return None


def list_zones() -> list[str]:
    """Available IANA zone names (from the system tz database)."""
    import zoneinfo

    return sorted(zoneinfo.available_timezones())


def _fmt_off(off: timedelta) -> str:
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def format_offset(off: timedelta) -> str:
    return _fmt_off(off)