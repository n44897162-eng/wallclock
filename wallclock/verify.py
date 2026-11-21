"""Temporal contracts and exhaustive verification.

The model
---------
A *schedule* produces a finite firing sequence over a window.  A *contract* is a
predicate over that sequence (and the local calendar it happens in).  Because
the window is finite, verification is **exhaustive**: every recurrence the rule
generates inside the window is resolved, every resulting firing is checked
against every contract.  "No findings" therefore means "no violation exists in
this window, under this tzdata", not "the tests passed".

Two families of output:

* **contract violations** -- things the user explicitly asked to be true
  (``max_gap: 26h``, ``local_window: 08:00-10:00``, ...).  Always ``error``.
* **anomalies** -- structural facts about the schedule/zone interaction that are
  worth knowing even without a contract: an occurrence vanished into a DST gap,
  a wall-clock minute happened twice, a run landed on the wrong calendar day,
  the UTC spacing of a "regular" job wobbled, or a cron day field silently used
  OR semantics.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Sequence

from .cron import parse_cron
from .errors import HorizonError, SpecError
from .schedule import Firing, Schedule
from .tz import Transition, Zone, format_offset, get_zone, tzdata_version

UTC = timezone.utc
SECOND = timedelta(seconds=1)
SEVERITIES = ("info", "warning", "error")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}
_MAX_FINDINGS_PER_CODE = 40

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)([smhdwy])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "y": 365 * 86400}


# ---------------------------------------------------------------------------
# small parsers
# ---------------------------------------------------------------------------
def parse_duration(text: str | int | float) -> timedelta:
    """``26h``, ``1d2h``, ``90m``, ``45s`` ... -> timedelta."""
    if isinstance(text, timedelta):
        return text
    if isinstance(text, (int, float)):
        return timedelta(seconds=float(text))
    raw = str(text).strip().lower().replace(" ", "")
    if not raw:
        raise SpecError("empty duration")
    pos = 0
    total = 0.0
    for m in _DURATION_RE.finditer(raw):
        if m.start() != pos:
            raise SpecError(f"bad duration {text!r} (use e.g. 26h, 1d2h, 90m)")
        total += float(m.group(1)) * _UNIT_SECONDS[m.group(2)]
        pos = m.end()
    if pos != len(raw) or total < 0:
        raise SpecError(f"bad duration {text!r} (use e.g. 26h, 1d2h, 90m)")
    return timedelta(seconds=total)


def format_duration(delta: timedelta) -> str:
    total = int(round(delta.total_seconds()))
    sign = "-" if total < 0 else ""
    total = abs(total)
    if total == 0:
        return "0s"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    parts = []
    if days >= 365:
        years, days = divmod(days, 365)
        parts.append(f"{years}y")
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds and not days:
        parts.append(f"{seconds}s")
    return sign + "".join(parts)


def parse_local_window(text: str) -> tuple[time, time]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", text or "")
    if not m:
        raise SpecError(f"bad local_window {text!r} (use HH:MM-HH:MM)")
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if not (0 <= h1 < 24 and 0 <= h2 < 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
        raise SpecError(f"bad local_window {text!r}")
    start, end = time(h1, m1), time(h2, m2)
    if start >= end:
        raise SpecError("local_window must not cross midnight (start must be before end)")
    return start, end


# ---------------------------------------------------------------------------
# findings and reports
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    code: str
    severity: str
    message: str
    at: datetime | None = None
    evidence: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in _RANK:
            raise ValueError(f"bad severity {self.severity!r}")

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "at": self.at.isoformat() if self.at else None,
            "evidence": _jsonable(self.evidence),
        }


@dataclass
class Simulation:
    schedule: Schedule
    lo: datetime
    hi: datetime
    firings: list[Firing]
    skipped: list[datetime]  # wall-clock occurrences that produced no instant
    recurrences: int
    transitions: list[Transition]

    @property
    def zone(self) -> Zone:
        return self.schedule.zone


@dataclass
class Report:
    simulation: Simulation
    contracts: list["Contract"]
    findings: list[Finding]
    tzdata: str | None = None

    @property
    def schedule(self) -> Schedule:
        return self.simulation.schedule

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    def by_code(self) -> dict[str, int]:
        return dict(Counter(f.code for f in self.findings))

    def to_dict(self) -> dict:
        sim = self.simulation
        return {
            "ok": self.ok,
            "tzdata": self.tzdata,
            "schedule": self.schedule.to_dict(),
            "schedule_text": self.schedule.describe(),
            "window": {"from": sim.lo.isoformat(), "to": sim.hi.isoformat()},
            "counts": {
                "recurrences": sim.recurrences,
                "firings": len(sim.firings),
                "skipped": len(sim.skipped),
                "transitions": len(sim.transitions),
            },
            "contracts": [c.describe() for c in self.contracts],
            "findings": [f.to_dict() for f in self.findings],
        }

    def summary(self) -> str:
        sim = self.simulation
        head = "PASS" if self.ok else "FAIL"
        return (
            f"{head}: {len(sim.firings)} firings, {len(sim.skipped)} skipped, "
            f"{len(self.errors)} errors, {len(self.warnings)} warnings"
        )


# ---------------------------------------------------------------------------
# contracts
# ---------------------------------------------------------------------------
class Contract:
    code = "contract"

    def check(self, sim: Simulation) -> list[Finding]:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class MaxGap(Contract):
    limit: timedelta
    code = "max_gap"

    @property
    def describe_text(self) -> str:
        return f"consecutive runs at most {format_duration(self.limit)} apart"

    def describe(self) -> str:
        return f"max_gap: {format_duration(self.limit)}"

    def check(self, sim: Simulation) -> list[Finding]:
        out: list[Finding] = []
        for a, b in zip(sim.firings, sim.firings[1:]):
            gap = b.utc - a.utc
            if gap > self.limit:
                out.append(
                    Finding(
                        "max_gap",
                        "error",
                        f"gap of {format_duration(gap)} between {_iso(a.utc)} and {_iso(b.utc)} "
                        f"exceeds {format_duration(self.limit)}",
                        at=b.utc,
                        evidence={"from": a.utc, "to": b.utc, "gap_seconds": int(gap.total_seconds()),
                                  "from_local": a.effective_local, "to_local": b.effective_local},
                    )
                )
        if len(sim.firings) < 2 and sim.recurrences > 1:
            out.append(
                Finding("max_gap", "error",
                        "fewer than two runs in the window, so no gap can satisfy the contract",
                        evidence={"firings": len(sim.firings)})
            )
        return out


@dataclass
class LocalWindow(Contract):
    start: time
    end: time
    code = "local_window"

    def describe(self) -> str:
        return f"local_window: {self.start:%H:%M}-{self.end:%H:%M}"

    def check(self, sim: Simulation) -> list[Finding]:
        out: list[Finding] = []
        for f in sim.firings:
            t = f.effective_local.time()
            if not (self.start <= t < self.end):
                out.append(
                    Finding(
                        "local_window",
                        "error",
                        f"run at {_iso(f.utc)} lands at {t:%H:%M} local, outside "
                        f"{self.start:%H:%M}-{self.end:%H:%M}",
                        at=f.utc,
                        evidence={"local": f.effective_local, "kind": f.kind},
                    )
                )
        return out


@dataclass
class MinPerDay(Contract):
    minimum: int
    code = "min_per_day"

    def describe(self) -> str:
        return f"min_per_day: {self.minimum}"

    def check(self, sim: Simulation) -> list[Finding]:
        counts = Counter(f.effective_local.date() for f in sim.firings)
        first, last = _active_dates(sim)
        out: list[Finding] = []
        for day in full_days(sim):
            if first is not None and day < first:
                continue
            if last is not None and day > last:
                continue
            if counts[day] < self.minimum:
                out.append(
                    Finding(
                        "min_per_day",
                        "error",
                        f"{day.isoformat()} has {counts[day]} runs, contract requires at least {self.minimum}",
                        at=None,
                        evidence={"date": day.isoformat(), "runs": counts[day]},
                    )
                )
        return out


@dataclass
class MaxPerDay(Contract):
    maximum: int
    code = "max_per_day"

    def describe(self) -> str:
        return f"max_per_day: {self.maximum}"

    def check(self, sim: Simulation) -> list[Finding]:
        counts = Counter(f.effective_local.date() for f in sim.firings)
        out: list[Finding] = []
        for day, n in sorted(counts.items()):
            if n > self.maximum:
                out.append(
                    Finding(
                        "max_per_day",
                        "error",
                        f"{day.isoformat()} has {n} runs, contract allows at most {self.maximum}",
                        at=None,
                        evidence={"date": day.isoformat(), "runs": n},
                    )
                )
        return out


@dataclass
class MaxDrift(Contract):
    limit: timedelta
    code = "max_drift"

    def describe(self) -> str:
        return f"max_drift: {format_duration(self.limit)}"

    def check(self, sim: Simulation) -> list[Finding]:
        out: list[Finding] = []
        for f in sim.firings:
            if abs(f.drift) > self.limit:
                out.append(
                    Finding(
                        "max_drift",
                        "error",
                        f"run at {_iso(f.utc)} landed {format_duration(f.drift)} away from the "
                        f"requested wall clock ({f.local:%Y-%m-%d %H:%M})",
                        at=f.utc,
                        evidence={"local": f.local, "effective_local": f.effective_local,
                                  "drift_seconds": int(f.drift.total_seconds()), "kind": f.kind},
                    )
                )
        return out


def parse_contract(spec: dict | str) -> Contract:
    if not isinstance(spec, dict) or len(spec) != 1:
        raise SpecError(f"a contract must be a single-key mapping, got {spec!r}")
    key, value = next(iter(spec.items()))
    key = str(key).lower()
    if key == "max_gap":
        return MaxGap(parse_duration(value))
    if key == "local_window":
        start, end = parse_local_window(str(value))
        return LocalWindow(start, end)
    if key == "min_per_day":
        return MinPerDay(_positive_int(value, key))
    if key == "max_per_day":
        return MaxPerDay(_positive_int(value, key))
    if key == "max_drift":
        return MaxDrift(parse_duration(value))
    raise SpecError(f"unknown contract {key!r} (max_gap, local_window, min_per_day, max_per_day, max_drift)")


def parse_contracts(specs: Sequence[dict | str]) -> list[Contract]:
    return [parse_contract(s) for s in specs]


# ---------------------------------------------------------------------------
# simulation + anomalies
# ---------------------------------------------------------------------------
def simulate(
    schedule: Schedule, lo: datetime, hi: datetime, max_firings: int = 1_500_000
) -> Simulation:
    lo, hi = _aware(lo), _aware(hi)
    if hi <= lo:
        raise HorizonError("verification window is empty")
    local_lo = schedule.zone.wall(lo)
    local_hi = schedule.zone.wall(hi)
    firings: list[Firing] = []
    skipped: list[datetime] = []
    recurrences = 0
    for seq, local, resolutions in schedule.expand(lo, hi):
        recurrences += 1
        kept = [r for r in resolutions if lo <= r.utc < hi]
        if len(firings) + len(kept) > max_firings:
            raise HorizonError(
                f"window produces more than {max_firings:,} runs; narrow the window "
                f"or use a coarser schedule"
            )
        for res in kept:
            firings.append(
                Firing(
                    utc=res.utc,
                    local=res.local,
                    effective_local=schedule.zone.wall(res.utc),
                    kind=res.kind,
                    offset=res.offset,
                    sequence=seq,
                )
            )
        if not resolutions and local_lo <= local < local_hi:
            skipped.append(local)
    firings.sort(key=lambda f: (f.utc, f.sequence))
    transitions = schedule.zone.transitions(lo - timedelta(days=2), hi + timedelta(days=2))
    return Simulation(schedule, lo, hi, firings, skipped, recurrences, transitions)


def _active_dates(sim: Simulation) -> tuple[date | None, date | None]:
    """Local dates outside which a finite RRULE cannot fire at all."""
    rule = sim.schedule.rrule
    if rule is None:
        return None, None
    first = rule.dtstart.date()
    last: date | None = None
    if rule.until is not None or rule.count is not None:
        end = rule.last_local(sim.zone.wall(sim.hi) + timedelta(days=2))
        if end is not None:
            last = end.date()
    return first, last


def full_days(sim: Simulation) -> list[date]:
    """Local dates fully covered by the window (used by per-day contracts)."""
    out: list[date] = []
    day = sim.zone.wall(sim.lo).date()
    last = sim.zone.wall(sim.hi).date()
    while day <= last:
        start_utc = sim.zone.resolve(datetime.combine(day, time(0, 0)), _clamp_policy())[0].utc
        next_day = day + timedelta(days=1)
        end_utc = sim.zone.resolve(datetime.combine(next_day, time(0, 0)), _clamp_policy())[0].utc
        if sim.lo <= start_utc and end_utc <= sim.hi:
            out.append(day)
        day = next_day
    return out


def _clamp_policy():
    from .tz import Policy

    return Policy(gap="clamp_end", ambiguous="earlier")


def detect_anomalies(sim: Simulation) -> list[Finding]:
    out: list[Finding] = []
    zone = sim.zone
    schedule = sim.schedule
    rrule = schedule.rrule

    # 1. occurrences that never happened (grouped: one finding per gap)
    groups: dict[Transition | None, list[datetime]] = {}
    for local in sim.skipped:
        groups.setdefault(_transition_for(zone, local, sim.transitions), []).append(local)
    for tr, dropped in groups.items():
        first, last = dropped[0], dropped[-1]
        when = (
            f" (clocks jump from {format_offset(tr.before)} to {format_offset(tr.after)} at {_iso(tr.at)})"
            if tr
            else ""
        )
        quota = ""
        if rrule is not None and rrule.count is not None:
            quota = "; each still consumes one of the RRULE COUNT recurrences"
        if len(dropped) == 1:
            what = f"{first:%Y-%m-%d %H:%M} does not exist in {zone.name}"
        else:
            what = (
                f"{len(dropped)} consecutive occurrences ({first:%Y-%m-%d %H:%M}"
                f"..{last:%H:%M}) do not exist in {zone.name}"
            )
        out.append(
            Finding(
                "skipped_occurrence",
                "warning",
                f"{what}{when}; the '{schedule.policy.gap}' policy drops "
                f"{'the run' if len(dropped) == 1 else 'all of them'}{quota}",
                at=tr.at if tr else None,
                evidence={
                    "local": first,
                    "last_local": last if len(dropped) > 1 else None,
                    "count": len(dropped),
                    "gap_seconds": int(tr.gap.total_seconds()) if tr else None,
                    "transition": _iso(tr.at) if tr else None,
                },
            )
        )

    # 2. wall-clock times that happened twice
    ambiguous = [f for f in sim.firings if f.ambiguous]
    if ambiguous:
        if schedule.policy.ambiguous == "both":
            out.append(
                Finding(
                    "double_fire",
                    "warning",
                    f"{len(ambiguous)} runs come from wall-clock times that occur twice "
                    f"(the 'both' policy fires once per occurrence)",
                    at=ambiguous[0].utc,
                    evidence={"first_local": ambiguous[0].local, "count": len(ambiguous)},
                )
            )
        else:
            out.append(
                Finding(
                    "ambiguous_occurrence",
                    "info",
                    f"{len(ambiguous)} wall-clock occurrences are ambiguous; the "
                    f"'{schedule.policy.ambiguous}' policy picked one instant",
                    at=ambiguous[0].utc,
                    evidence={"first_local": ambiguous[0].local, "count": len(ambiguous)},
                )
            )

    # 3. runs that land on a different calendar day
    shifted = [f for f in sim.firings if f.effective_local.date() != f.local.date()]
    for f in shifted[:5]:
        out.append(
            Finding(
                "date_shift",
                "warning",
                f"requested {f.local:%Y-%m-%d %H:%M} local ran at "
                f"{f.effective_local:%Y-%m-%d %H:%M} local (different date)",
                at=f.utc,
                evidence={"local": f.local, "effective_local": f.effective_local, "kind": f.kind},
            )
        )

    # 4. UTC spacing wobble for otherwise-regular schedules
    out.extend(_jitter(sim))

    # 5. nothing fired at all
    if not sim.firings:
        if sim.recurrences == 0:
            out.append(
                Finding(
                    "never_fires",
                    "error",
                    "the rule never matches a wall-clock time in this window "
                    "(an impossible date such as Feb 30, or an exhausted COUNT/UNTIL)",
                    evidence={"recurrences": 0, "skipped": 0},
                )
            )
        elif sim.skipped:
            out.append(
                Finding(
                    "never_fires",
                    "warning",
                    f"all {len(sim.skipped)} matched occurrences were dropped by the "
                    f"'{schedule.policy.gap}' policy",
                    evidence={"recurrences": sim.recurrences, "skipped": len(sim.skipped)},
                )
            )

    # 6. the cron DOM/DOW OR trap, with evidence
    cron = schedule.cron
    if cron is not None and cron.or_semantics():
        extra = [f for f in sim.firings if cron.only_one_of_dom_dow(f.local.date())]
        if extra:
            out.append(
                Finding(
                    "cron_or_extra",
                    "warning",
                    f"{len(extra)} of {len(sim.firings)} runs match only one of day-of-month "
                    f"and day-of-week: cron ORs them when neither field is '*', so e.g. "
                    f"{extra[0].local:%Y-%m-%d} runs because it is one of the two, not both",
                    at=extra[0].utc,
                    evidence={"extra_runs": len(extra), "first": extra[0].local},
                )
            )

    # 7. specification hazards the parser can see
    for text in schedule.warnings():
        out.append(Finding("spec", "warning", text))

    return out


def _jitter(sim: Simulation) -> list[Finding]:
    if len(sim.firings) < 4:
        return []
    gaps = [b.utc - a.utc for a, b in zip(sim.firings, sim.firings[1:])]
    modal, modal_n = Counter(gaps).most_common(1)[0]
    if modal_n < 0.6 * len(gaps):
        return []  # not a regular schedule; a "wobble" is meaningless
    out: list[Finding] = []
    for (a, b), gap in zip(zip(sim.firings, sim.firings[1:]), gaps):
        if abs(gap - modal) > timedelta(seconds=60):
            out.append(
                Finding(
                    "utc_jitter",
                    "info",
                    f"spacing of {format_duration(gap)} between {_iso(a.utc)} and {_iso(b.utc)} "
                    f"differs from the usual {format_duration(modal)}",
                    at=b.utc,
                    evidence={"from": a.utc, "to": b.utc, "gap_seconds": int(gap.total_seconds()),
                              "modal_seconds": int(modal.total_seconds())},
                )
            )
            if len(out) >= 5:
                break
    return out


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def verify(
    schedule: Schedule,
    contracts: Sequence[Contract] | Sequence[dict] = (),
    lo: datetime | None = None,
    hi: datetime | None = None,
    max_firings: int = 1_500_000,
) -> Report:
    """Exhaustively check ``contracts`` over ``[lo, hi)``."""
    if lo is None or hi is None:
        raise HorizonError("verify() needs a window (lo, hi)")
    sim = simulate(schedule, lo, hi, max_firings=max_firings)
    parsed: list[Contract] = []
    for c in contracts:
        parsed.append(c if isinstance(c, Contract) else parse_contract(c))
    findings: list[Finding] = []
    for contract in parsed:
        found = contract.check(sim)
        if len(found) > _MAX_FINDINGS_PER_CODE:
            extra = len(found) - _MAX_FINDINGS_PER_CODE
            found = found[:_MAX_FINDINGS_PER_CODE]
            found.append(
                Finding(contract.code, "error", f"... and {extra} more {contract.code} violations",
                        evidence={"truncated": extra})
            )
        findings.extend(found)
    findings.extend(detect_anomalies(sim))
    findings.sort(key=lambda f: (-_RANK[f.severity], f.at or datetime.max.replace(tzinfo=UTC), f.code))
    return Report(sim, parsed, findings, tzdata_version())


def horizon(zone: str, start: str, end: str) -> tuple[datetime, datetime]:
    """Turn ``2026-01-01`` style bounds into UTC instants in a zone."""
    z = get_zone(zone)

    def one(value) -> datetime:
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value.astimezone(UTC)
            return z.resolve(value, _clamp_policy())[0].utc
        if isinstance(value, date):
            value = value.isoformat()
        text = str(value).strip()
        if text.endswith("Z"):
            return datetime.fromisoformat(text[:-1] + "+00:00").astimezone(UTC)
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            return dt.astimezone(UTC)
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            local = dt
        else:
            local = dt
        resolutions = z.resolve(local, _clamp_policy())
        return resolutions[0].utc

    lo, hi = one(start), one(end)
    if hi <= lo:
        raise HorizonError("window end must be after its start")
    return lo, hi


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _aware(dt: datetime) -> datetime:
    if not isinstance(dt, datetime):
        raise HorizonError("expected a datetime")
    if dt.tzinfo is None:
        raise HorizonError("expected a timezone-aware datetime")
    return dt.astimezone(UTC)


def _positive_int(value, name: str) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise SpecError(f"{name} must be an integer") from None
    if v < 0:
        raise SpecError(f"{name} must be >= 0")
    return v


def _transition_for(zone: Zone, local: datetime, transitions: Sequence[Transition]) -> Transition | None:
    for tr in transitions:
        if not tr.is_gap:
            continue
        start = zone.wall(tr.at) - tr.gap
        if start <= local < zone.wall(tr.at):
            return tr
    return None


def _local_noon_utc(sim: Simulation, day: date) -> datetime:
    resolutions = sim.zone.resolve(datetime.combine(day, time(12, 0)), _clamp_policy())
    return resolutions[0].utc if resolutions else sim.lo


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _jsonable(value):
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, timedelta):
        return format_duration(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return value