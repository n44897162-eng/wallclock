"""Semantic diff between two schedules.

``git diff`` on a crontab tells you the text changed.  It does not tell you that
the change silently removed 365 runs, or moved one run by an hour, or turned a
job that used to survive DST into one that does not.  This module answers the
question a reviewer actually has: *what happens differently in the next N
months?*

Matching is by instant first (a run that did not move is unchanged), then by
nearest instant within a tolerance (a run that moved), then the rest are added
or removed.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .schedule import Firing, Schedule
from .verify import format_duration, simulate


@dataclass
class Diff:
    a: Schedule
    b: Schedule
    lo: datetime
    hi: datetime
    common: list[Firing] = field(default_factory=list)
    moved: list[tuple[Firing, Firing]] = field(default_factory=list)
    added: list[Firing] = field(default_factory=list)
    removed: list[Firing] = field(default_factory=list)
    tolerance: timedelta = timedelta(hours=3)

    @property
    def changed(self) -> int:
        return len(self.moved) + len(self.added) + len(self.removed)

    def summary(self) -> str:
        bits = [f"{len(self.common)} unchanged"]
        if self.moved:
            deltas = sorted({d for _, _, d in self._moved_with_delta()})
            bits.append(f"{len(self.moved)} moved ({', '.join(format_duration(d) for d in deltas[:3])})")
        if self.added:
            bits.append(f"{len(self.added)} added")
        if self.removed:
            bits.append(f"{len(self.removed)} removed")
        return ", ".join(bits)

    def _moved_with_delta(self) -> list[tuple[Firing, Firing, timedelta]]:
        return [(x, y, y.utc - x.utc) for x, y in self.moved]

    def to_dict(self) -> dict:
        return {
            "a": self.a.describe(),
            "b": self.b.describe(),
            "window": {"from": self.lo.isoformat(), "to": self.hi.isoformat()},
            "counts": {
                "unchanged": len(self.common),
                "moved": len(self.moved),
                "added": len(self.added),
                "removed": len(self.removed),
            },
            "summary": self.summary(),
            "moved": [
                {"from": x.utc.isoformat(), "to": y.utc.isoformat(), "delta_seconds": int((y.utc - x.utc).total_seconds())}
                for x, y in self.moved[:20]
            ],
            "added": [f.utc.isoformat() for f in self.added[:20]],
            "removed": [f.utc.isoformat() for f in self.removed[:20]],
        }


def diff(a: Schedule, b: Schedule, lo: datetime, hi: datetime, tolerance: timedelta = timedelta(hours=3)) -> Diff:
    """Compare the firings of two schedules over one window."""
    if lo.tzinfo is None or hi.tzinfo is None:
        from .errors import HorizonError

        raise HorizonError("diff() needs timezone-aware bounds")
    fa = simulate(a, lo, hi).firings
    fb = simulate(b, lo, hi).firings
    out = Diff(a, b, lo, hi, tolerance=tolerance)

    b_by_instant = {f.utc: f for f in fb}
    unmatched_a: list[Firing] = []
    used_b: set[datetime] = set()
    for f in fa:
        if f.utc in b_by_instant:
            out.common.append(f)
            used_b.add(f.utc)
        else:
            unmatched_a.append(f)

    remaining_b = [f for f in fb if f.utc not in used_b]
    remaining_b.sort(key=lambda f: f.utc)
    b_times = [f.utc for f in remaining_b]
    claimed: set[int] = set()

    for f in unmatched_a:
        idx = bisect.bisect_left(b_times, f.utc)
        best: int | None = None
        best_delta: timedelta | None = None
        for j in (idx - 1, idx):
            if 0 <= j < len(remaining_b) and j not in claimed:
                delta = remaining_b[j].utc - f.utc
                if abs(delta) <= tolerance and (best_delta is None or abs(delta) < abs(best_delta)):
                    best, best_delta = j, delta
        if best is None:
            out.removed.append(f)
        else:
            claimed.add(best)
            out.moved.append((f, remaining_b[best]))

    out.added = [f for j, f in enumerate(remaining_b) if j not in claimed]
    for seq in (out.common, out.added, out.removed):
        seq.sort(key=lambda f: f.utc)
    out.moved.sort(key=lambda pair: pair[0].utc)
    return out