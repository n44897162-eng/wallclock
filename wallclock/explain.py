"""Human-readable explanations for every contract and finding code.

The codes are short by design (they travel through JSON and CI), which makes
them opaque to a first-time reader.  ``wallclock explain <code>`` is the bridge:
one paragraph on what the code claims, and one runnable command that
demonstrates it.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass

from .errors import WallclockError


@dataclass(frozen=True)
class Entry:
    code: str
    kind: str  # "contract" | "finding"
    summary: str
    detail: str
    example: str

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "kind": self.kind,
            "summary": self.summary,
            "detail": self.detail,
            "example": self.example,
        }


_ENTRIES: tuple[Entry, ...] = (
    Entry(
        "max_gap",
        "contract",
        "consecutive runs are never further apart than this",
        "The only contract about absolute time rather than the calendar. A dropped daily "
        "occurrence does not create a 24-hour gap, it creates a 47-hour one, because the "
        "next run keeps its own wall-clock slot. This is the contract that catches jobs "
        "which call themselves daily and are not.",
        "wallclock check --cron '30 2 * * *' --tz Europe/Berlin "
        "--from 2026-03-01 --to 2026-04-01 --max-gap 26h",
    ),
    Entry(
        "local_window",
        "contract",
        "every run lands inside a local time-of-day window",
        "Judged on the effective local time (what the clock reads at the instant), so a "
        "UTC-anchored schedule drifts out of the window when the offset changes. The window "
        "must not cross midnight; use forbidden_window for that.",
        "wallclock check --cron '30 2 * * *' --tz Europe/Berlin --anchor utc "
        "--from 2026-01-01 --to 2026-07-01 --local-window 02:00-03:00",
    ),
    Entry(
        "forbidden_window",
        "contract",
        "no run lands inside a local range, which may cross midnight",
        "The complement of local_window and the one that can wrap: 22:00-06:00 means "
        "'never at night'. Useful for change freezes, quiet hours and any policy where "
        "the danger is a run happening rather than not happening.",
        "wallclock check --cron '0 2 * * *' --forbidden-window 22:00-06:00 "
        "--from 2026-01-01 --to 2026-01-08",
    ),
    Entry(
        "min_per_day",
        "contract",
        "at least N runs on every fully covered local date",
        "Only dates fully inside the window are judged, and dates before DTSTART or after a "
        "finite rule's UNTIL/COUNT are skipped, so window edges and finished rules do not "
        "create false positives.",
        "wallclock check --cron '30 2 * * *' --tz Europe/Berlin "
        "--from 2026-03-01 --to 2026-04-01 --min-per-day 1",
    ),
    Entry(
        "max_per_day",
        "contract",
        "at most N runs on any local date",
        "The natural way to catch a job that runs twice when the clock falls back.",
        "wallclock check --cron '30 2 * * *' --tz Europe/Berlin --ambiguous both "
        "--from 2026-10-20 --to 2026-11-01 --max-per-day 1",
    ),
    Entry(
        "min_per_week",
        "contract",
        "at least N runs in every fully covered ISO week",
        "Weeks are Monday-Sunday. A week that is only partially covered by the window, or "
        "that a finite rule was not alive for in full, is not judged.",
        "wallclock check --cron '0 9 * * 1-5' --tz Europe/London "
        "--from 2026-01-01 --to 2026-04-01 --min-per-week 5",
    ),
    Entry(
        "max_per_week",
        "contract",
        "at most N runs in every fully covered ISO week",
        "Useful for rate limits that are counted per calendar week rather than per day.",
        "wallclock check --cron '0 12 * * *' --from 2026-01-05 --to 2026-02-02 --max-per-week 6",
    ),
    Entry(
        "max_drift",
        "contract",
        "a run may not land further than this from the wall clock it asked for",
        "Catches resolution policies and anchoring: a 02:30 job that fires at 03:00 has "
        "30 minutes of drift, and a UTC-anchored 02:30 job has an hour of drift all summer.",
        "wallclock check --cron '30 2 * * *' --tz Europe/Berlin --gap clamp_end "
        "--from 2026-03-28 --to 2026-03-31 --max-drift 0s",
    ),
    Entry(
        "skipped_occurrence",
        "finding",
        "a wall-clock time did not exist, so the occurrence was dropped",
        "A spring-forward gap. Findings are grouped per gap: one finding carries the count "
        "and the first/last local time. If the rule has COUNT, every dropped occurrence "
        "still consumes one of the quota.",
        "wallclock check --cron '* * * * *' --tz Europe/Berlin "
        "--from 2026-03-29 --to 2026-03-30",
    ),
    Entry(
        "ambiguous_occurrence",
        "finding",
        "a wall-clock time existed twice, and one instant was used",
        "A fall-back overlap. The policy (earlier/later) picks one instant; this finding "
        "tells you the choice was made.",
        "wallclock check --cron '30 2 * * *' --tz Europe/Berlin "
        "--from 2026-10-24 --to 2026-10-26",
    ),
    Entry(
        "double_fire",
        "finding",
        "both instants of a repeated wall-clock time fired",
        "Only happens with ambiguous=both (some schedulers do this). Two runs in one night "
        "is a real billing/notification hazard.",
        "wallclock check --cron '30 2 * * *' --tz Europe/Berlin --ambiguous both "
        "--from 2026-10-24 --to 2026-10-27",
    ),
    Entry(
        "date_shift",
        "finding",
        "the run landed on a different local calendar date",
        "Typical for UTC-anchored schedules across midnight, or for clamp_end when a whole "
        "local day was skipped (Samoa, 2011-12-30).",
        "wallclock check --cron '0 12 * * *' --tz Pacific/Apia --anchor utc "
        "--from 2026-01-01 --to 2026-01-05",
    ),
    Entry(
        "utc_jitter",
        "finding",
        "a regular schedule's absolute spacing wobbled",
        "Informational: compares each gap to the modal gap. A daily 02:30 job shows a "
        "47-hour gap at the spring transition and a 23-hour gap in the autumn.",
        "wallclock check --cron '30 2 * * *' --tz Europe/Berlin "
        "--from 2026-03-27 --to 2026-04-02",
    ),
    Entry(
        "never_fires",
        "finding",
        "nothing fires inside the window",
        "An impossible date (February 30), an RRULE whose COUNT/UNTIL ran out before the "
        "window, or every occurrence dropped by the gap policy.",
        "wallclock check --cron '0 0 30 2 *' --from 2026-01-01 --to 2027-01-01",
    ),
    Entry(
        "cron_or_extra",
        "finding",
        "cron's day-of-month OR day-of-week produced runs matching only one field",
        "Vixie semantics: when neither day field starts with '*', they are OR'd. "
        "`0 0 1 * 1` runs on the 1st and on every Monday.",
        "wallclock check --cron '0 0 1 * 1' --from 2026-01-01 --to 2026-04-01",
    ),
    Entry(
        "spec",
        "finding",
        "the specification itself is ambiguous",
        "Examples: DTSTART that does not satisfy its own BY* filters (RFC 5545 calls the "
        "first period undefined), or COUNT and UNTIL together. Reported instead of being "
        "silently resolved one way.",
        "wallclock check --rrule 'FREQ=WEEKLY;BYDAY=FR' --dtstart 2026-01-05T09:00 "
        "--from 2026-01-01 --to 2026-02-01",
    ),
)

ENTRIES: dict[str, Entry] = {entry.code: entry for entry in _ENTRIES}


def codes() -> list[str]:
    return [entry.code for entry in _ENTRIES]


def lookup(code: str) -> Entry:
    key = (code or "").strip().lower()
    if key in ENTRIES:
        return ENTRIES[key]
    close = difflib.get_close_matches(key, list(ENTRIES), n=3, cutoff=0.6)
    hint = f"; did you mean {', '.join(close)}?" if close else ""
    raise WallclockError(f"unknown code {code!r}{hint} (try `wallclock explain --list`)")


def render(entry: Entry) -> str:
    lines = [
        f"{entry.code}  [{entry.kind}]",
        f"  {entry.summary}",
        "",
        _wrap(entry.detail, 88, "  "),
        "",
        f"  example: {entry.example}",
    ]
    return "\n".join(lines)


def render_list() -> str:
    out: list[str] = []
    for kind, title in (("contract", "contracts"), ("finding", "findings")):
        out.append(f"{title}:")
        for entry in _ENTRIES:
            if entry.kind == kind:
                out.append(f"  {entry.code:18s} {entry.summary}")
        out.append("")
    out.append("`wallclock explain <code>` for the full text.")
    return "\n".join(out)


def render_json(code: str | None) -> str:
    if code:
        return json.dumps(lookup(code).to_dict(), indent=2, ensure_ascii=False)
    return json.dumps([entry.to_dict() for entry in _ENTRIES], indent=2, ensure_ascii=False)


def _wrap(text: str, width: int, prefix: str) -> str:
    words = text.split()
    lines: list[str] = []
    line = prefix
    for word in words:
        if len(line) + len(word) + 1 > width and line.strip():
            lines.append(line)
            line = prefix + word
        else:
            line += (" " if line.strip() else "") + word
    if line.strip():
        lines.append(line)
    return "\n".join(lines)