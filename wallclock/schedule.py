"""Schedules: a wall-clock rule + a zone + a resolution policy -> firings.

A :class:`Schedule` is the object every other module talks about.  It answers
exactly one question: *which instants does this job actually run?*  Everything
interesting about the project lives in the gap between the rule the user wrote
and the list this method returns:

* the rule matches wall-clock datetimes, which may not exist;
* some wall-clock datetimes exist twice;
* some schedulers anchor the recurrence in UTC instead of local time, which
  turns "every night at 02:30" into "every night at 01:30 or 02:30, depending
  on the season".

Both anchors are modelled because the difference is a real, common bug, and
"anchor this in UTC" is one of the repairs :mod:`wallclock.repair` proposes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator, Sequence

from . import cron as cron_mod
from . import rrule as rrule_mod
from .errors import HorizonError, SpecError
from .tz import DEFAULT_POLICY, Policy, Resolution, Zone, get_zone

UTC = timezone.utc
#: generous margin (in wall-clock time) used when expanding a rule for a UTC window
_EXPAND = timedelta(days=2)
_MAX_IDS = 2_000_000


@dataclass(frozen=True)
class Firing:
    """One actual run of a job."""

    utc: datetime
    local: datetime  # wall clock the rule asked for
    effective_local: datetime  # what the clock will actually read
    kind: str  # normal | ambiguous_* | gap_* | utc
    offset: timedelta
    sequence: int

    @property
    def shifted(self) -> bool:
        return self.kind.startswith("gap_")

    @property
    def ambiguous(self) -> bool:
        return self.kind.startswith("ambiguous_")

    @property
    def drift(self) -> timedelta:
        """How far the real clock is from the requested wall clock."""
        return self.effective_local - self.local

    def describe(self) -> str:
        return f"{self.utc.isoformat()} ({self.effective_local.isoformat()} local, {self.kind})"


@dataclass(frozen=True)
class Schedule:
    """A recurring job, fully specified."""

    kind: str  # "cron" | "rrule"
    zone: Zone
    cron: cron_mod.Cron | None = None
    rrule: rrule_mod.RRule | None = None
    policy: Policy = DEFAULT_POLICY
    anchor: str = "local"  # "local" | "utc"

    def __post_init__(self) -> None:
        if self.kind not in ("cron", "rrule"):
            raise SpecError(f"unknown schedule kind {self.kind!r}")
        if self.kind == "cron" and self.cron is None:
            raise SpecError("cron schedule needs a parsed expression")
        if self.kind == "rrule" and self.rrule is None:
            raise SpecError("rrule schedule needs a parsed rule")
        if self.anchor not in ("local", "utc"):
            raise SpecError(f"unknown anchor {self.anchor!r} (use 'local' or 'utc')")

    # -- constructors ------------------------------------------------------
    @classmethod
    def from_cron(
        cls,
        expr: str,
        timezone: str,
        policy: Policy = DEFAULT_POLICY,
        anchor: str = "local",
    ) -> "Schedule":
        return cls(kind="cron", zone=get_zone(timezone), cron=cron_mod.parse_cron(expr),
                   policy=policy, anchor=anchor)

    @classmethod
    def from_rrule(
        cls,
        text: str,
        dtstart: datetime | str,
        timezone: str,
        policy: Policy = DEFAULT_POLICY,
        anchor: str = "local",
    ) -> "Schedule":
        zone = get_zone(timezone)
        start = _as_datetime(dtstart, zone)
        rule = rrule_mod.parse_rrule(_localise_until(text, zone), start)
        return cls(kind="rrule", zone=zone, rrule=rule, policy=policy, anchor=anchor)

    @classmethod
    def from_dict(cls, spec: dict) -> "Schedule":
        """Build from a config mapping (the YAML/JSON shape used by the CLI)."""
        if not isinstance(spec, dict):
            raise SpecError("schedule must be a mapping")
        tz_name = spec.get("timezone") or spec.get("tz")
        if not tz_name:
            raise SpecError("schedule needs a 'timezone'")
        policy_d = spec.get("policy") or {}
        if isinstance(policy_d, str):
            raise SpecError("policy must be a mapping like {gap: skip, ambiguous: earlier}")
        policy = Policy(
            gap=policy_d.get("gap", DEFAULT_POLICY.gap),
            ambiguous=policy_d.get("ambiguous", DEFAULT_POLICY.ambiguous),
        )
        anchor = spec.get("anchor", "local")
        has_cron = "cron" in spec
        has_rrule = "rrule" in spec
        if has_cron == has_rrule:
            raise SpecError("schedule needs exactly one of 'cron' or 'rrule'")
        if has_cron:
            return cls.from_cron(str(spec["cron"]), tz_name, policy=policy, anchor=anchor)
        dtstart = spec.get("dtstart")
        if not dtstart:
            raise SpecError("rrule schedule needs a 'dtstart'")
        return cls.from_rrule(str(spec["rrule"]), dtstart, tz_name, policy=policy, anchor=anchor)

    # -- simulation --------------------------------------------------------
    def fire(self, lo: datetime, hi: datetime) -> list[Firing]:
        """All firings in the UTC half-open window ``[lo, hi)``."""
        out: list[Firing] = []
        for seq, _local, resolutions in self.expand(lo, hi):
            for res in resolutions:
                if lo <= res.utc < hi:
                    out.append(
                        Firing(
                            utc=res.utc,
                            local=res.local,
                            effective_local=self.zone.wall(res.utc),
                            kind=res.kind,
                            offset=res.offset,
                            sequence=seq,
                        )
                    )
        out.sort(key=lambda f: (f.utc, f.sequence))
        return out

    def expand(
        self, lo: datetime, hi: datetime
    ) -> Iterator[tuple[int, datetime, list[Resolution]]]:
        """Yield ``(sequence, recurrence id, resolutions)`` around the window.

        Resolutions are *not* filtered by the window (the expansion runs with a
        two-day margin so that a zone offset can never pull a firing in or out
        silently); callers filter on ``resolution.utc``.  An empty list means
        the wall-clock occurrence does not exist and the policy dropped it --
        exactly the information ``fire()`` throws away and the verifier needs.
        """
        if lo.tzinfo is None or hi.tzinfo is None:
            raise HorizonError("expand() needs timezone-aware bounds")
        if hi <= lo:
            return
        seq = 0
        for local in self._recurrence_ids(lo, hi):
            seq += 1
            yield seq, local, self._resolve(local)

    def fire_all(self, lo: datetime, hi: datetime, limit: int = 100_000) -> list[Firing]:
        firings = self.fire(lo, hi)
        if len(firings) > limit:
            raise HorizonError(f"{len(firings)} firings exceeds limit {limit}; narrow the horizon")
        return firings

    def _recurrence_ids(self, lo: datetime, hi: datetime) -> Iterator[datetime]:
        local_lo = self.zone.wall(lo) - _EXPAND
        local_hi = self.zone.wall(hi) + _EXPAND
        if self.anchor == "utc":
            local_lo = lo.replace(tzinfo=None) - _EXPAND
            local_hi = hi.replace(tzinfo=None) + _EXPAND
        if self.kind == "cron":
            assert self.cron is not None
            yield from self.cron.iter_local(local_lo, local_hi)
            return
        assert self.rrule is not None
        made = 0
        for dt in self.rrule.iter_local(local_hi):
            if dt < local_lo:
                made += 1
                if made > _MAX_IDS:
                    raise HorizonError("recurrence has too many instances before the horizon")
                continue
            yield dt

    def _resolve(self, local: datetime) -> list[Resolution]:
        if self.anchor == "utc":
            utc = local.replace(tzinfo=UTC)
            return [Resolution(local=local, utc=utc, kind="utc", offset=self.zone.offset_at(utc))]
        return self.zone.resolve(local, self.policy)

    # -- description -------------------------------------------------------
    def to_dict(self) -> dict:
        out: dict = {"timezone": self.zone.name, "anchor": self.anchor,
                     "policy": {"gap": self.policy.gap, "ambiguous": self.policy.ambiguous}}
        if self.kind == "cron":
            assert self.cron is not None
            out["cron"] = self.cron.expr
        else:
            assert self.rrule is not None
            out["rrule"] = self.rrule.raw
            out["dtstart"] = self.rrule.dtstart.isoformat(timespec="minutes")
        return out

    def describe(self) -> str:
        rule = self.cron.expr if self.kind == "cron" else self.rrule.raw  # type: ignore[union-attr]
        base = f"{'cron' if self.kind == 'cron' else 'rrule'} {rule!r} in {self.zone.name}"
        if self.anchor == "utc":
            return base + " (anchored in UTC)"
        return base + f" ({self.policy.describe()})"

    def with_policy(self, policy: Policy) -> "Schedule":
        return replace(self, policy=policy)

    def with_anchor(self, anchor: str) -> "Schedule":
        return replace(self, anchor=anchor)

    def with_cron(self, expr: str) -> "Schedule":
        return replace(self, cron=cron_mod.parse_cron(expr), kind="cron")

    def with_rrule(self, text: str, dtstart: datetime | None = None) -> "Schedule":
        assert self.rrule is not None
        start = dtstart or self.rrule.dtstart
        return replace(self, kind="rrule", rrule=rrule_mod.parse_rrule(text, start))

    # -- self-knowledge ----------------------------------------------------
    def warnings(self) -> list[str]:
        """Things about the *specification* that deserve a human look."""
        out: list[str] = []
        if self.kind == "cron" and self.cron is not None and self.cron.or_semantics():
            out.append(
                "cron day fields are OR'd (neither DOM nor DOW starts with '*'): "
                "the job fires when *either* matches, which is rarely intended"
            )
        if self.kind == "rrule" and self.rrule is not None:
            rule = self.rrule
            if rule.count is not None and rule.until is not None:
                out.append("RRULE has both COUNT and UNTIL; the earlier bound wins")
            if rule.anchor_warning():
                out.append(rule.anchor_warning())
        return out


def _as_datetime(value: datetime | str, zone: Zone) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return zone.wall(value)
        return value.replace(second=0, microsecond=0)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise SpecError(f"bad DTSTART {value!r} (use ISO 8601, e.g. 2026-01-05T09:00)") from None
    if dt.tzinfo is not None:
        return zone.wall(dt)
    return dt.replace(second=0, microsecond=0)


_UNTIL_Z = re.compile(r"(UNTIL=\d{8}T\d{6}Z)", re.I)


def _localise_until(text: str, zone: Zone) -> str:
    """RRULE UNTIL with ``Z`` is an absolute instant; convert it to wall clock."""
    m = _UNTIL_Z.search(text)
    if not m:
        return text
    stamp = m.group(1).split("=", 1)[1][:-1]
    dt = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    local = zone.wall(dt).strftime("%Y%m%dT%H%M%S")
    return text.replace(m.group(1), f"UNTIL={local}")