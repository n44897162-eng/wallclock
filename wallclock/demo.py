"""The built-in tour: six small schedules that each break in a different way.

Every case is chosen because the *specification looks reasonable* and the
failure is silent: nothing crashes, no log line is written, the job just does
not run (or runs twice, or runs on the wrong day).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .report import render_html, render_ics
from .schedule import Schedule
from .tz import Policy
from .verify import Report, horizon, parse_contract, verify

UTC = timezone.utc


@dataclass
class Case:
    slug: str
    title: str
    blurb: str
    schedule: Schedule
    contracts: list
    lo: datetime
    hi: datetime

    def run(self) -> Report:
        return verify(self.schedule, self.contracts, self.lo, self.hi)


def cases() -> list[Case]:
    return [
        Case(
            "berlin-0230",
            "Berlin, every night at 02:30",
            "The most ordinary cron in Europe. On the last Sunday of March the hour between "
            "02:00 and 03:00 does not exist, so the nightly job is skipped -- and because it is "
            "daily, the next run is 47 hours later.",
            Schedule.from_cron("30 2 * * *", "Europe/Berlin"),
            [parse_contract({"max_gap": "26h"}), parse_contract({"min_per_day": 1})],
            *horizon("Europe/Berlin", "2026-03-01", "2026-04-01"),
        ),
        Case(
            "santiago-midnight",
            "Santiago, every night at 00:00",
            "Chile moves its clocks at midnight, so the midnight run itself disappears on the "
            "spring-forward Sunday. A 'daily' backup silently becomes non-daily once a year.",
            Schedule.from_cron("0 0 * * *", "America/Santiago"),
            [parse_contract({"min_per_day": 1})],
            *horizon("America/Santiago", "2026-08-15", "2026-10-01"),
        ),
        Case(
            "lord-howe-0215",
            "Lord Howe Island, 02:15 daily",
            "A 30-minute DST shift: 02:00 jumps to 02:30. A job at 02:15 falls inside the "
            "half-hour hole and vanishes, which 60-minute reasoning never predicts.",
            Schedule.from_cron("15 2 * * *", "Australia/Lord_Howe"),
            [parse_contract({"max_gap": "25h"})],
            *horizon("Australia/Lord_Howe", "2026-09-20", "2026-10-20"),
        ),
        Case(
            "samoa-skipped-day",
            "Samoa, 2011-12-30 (a day that never happened)",
            "Samoa jumped across the date line: the whole of 2011-12-30 was skipped. Every "
            "schedule in Pacific/Apia lost that day, and a yearly December 30 job lost a year.",
            Schedule.from_cron("0 12 * * *", "Pacific/Apia"),
            [parse_contract({"min_per_day": 1})],
            *horizon("Pacific/Apia", "2011-12-20", "2012-01-05"),
        ),
        Case(
            "leap-day-yearly",
            "February 29, every year",
            "A yearly recurrence anchored on a leap day can wait eight years: 2096 -> 2104, "
            "because 2100 is not a leap year. 'Yearly' freshness is a promise the calendar "
            "cannot keep.",
            Schedule.from_rrule(
                "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29", "2092-02-29T09:00", "UTC"
            ),
            [parse_contract({"max_gap": "5y"})],
            *horizon("UTC", "2090-01-01", "2112-01-01"),
        ),
        Case(
            "berlin-double-fire",
            "Berlin, 02:30, 'fire on both' policy",
            "The autumn clock change makes 02:00-03:00 happen twice. A scheduler configured "
            "to fire on both occurrences runs the job twice in one night -- which a "
            "once-per-day contract catches and a dashboard does not.",
            Schedule.from_cron("30 2 * * *", "Europe/Berlin", policy=Policy(ambiguous="both")),
            [parse_contract({"max_per_day": 1})],
            *horizon("Europe/Berlin", "2026-10-20", "2026-11-01"),
        ),
        Case(
            "cron-dom-dow",
            "cron '0 0 1 * 1' -- the day-field trap",
            "Cron ORs day-of-month and day-of-week when neither field is '*'. The intent was "
            "'the 1st, if it is a Monday'; the effect is 'the 1st, and every Monday'.",
            Schedule.from_cron("0 0 1 * 1", "UTC"),
            [parse_contract({"max_per_day": 1})],
            *horizon("UTC", "2026-01-01", "2026-04-01"),
        ),
    ]


def run_demo(out_dir: str | None = None, as_json: bool = False) -> int:
    reports = [(case, case.run()) for case in cases()]
    if as_json:
        payload = [
            {
                "slug": case.slug,
                "title": case.title,
                "blurb": case.blurb,
                "report": report.to_dict(),
            }
            for case, report in reports
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print("wallclock demo - seven silent scheduling failures\n" + "=" * 62)
    for case, report in reports:
        sim = report.simulation
        verdict = "PASS" if report.ok else "FAIL"
        print(f"\n[{verdict}] {case.title}")
        print(f"  {case.schedule.describe()}")
        print(f"  window {case.lo.date()} .. {case.hi.date()} | {len(sim.firings)} runs, "
              f"{len(sim.skipped)} skipped | {len(report.errors)}E {len(report.warnings)}W")
        print(f"  {case.blurb}")
        for finding in report.findings[:3]:
            print(f"    - {finding.severity}: {finding.message}")
        if len(report.findings) > 3:
            print(f"    ... {len(report.findings) - 3} more finding(s)")
    print("\nRun `wallclock check --help` to point this at your own schedules.")

    if out_dir:
        path = Path(out_dir)
        path.mkdir(parents=True, exist_ok=True)
        for case, report in reports:
            (path / f"{case.slug}.html").write_text(render_html(report, title=case.title), encoding="utf-8")
        flagship, flagship_report = reports[0]
        (path / f"{flagship.slug}.ics").write_text(
            render_ics(flagship_report.simulation.firings, flagship.schedule,
                       name=flagship.title, limit=2000),
            encoding="utf-8",
        )
        (path / "index.html").write_text(_index(reports), encoding="utf-8")
        print(f"HTML tour written to {path / 'index.html'}")
    return 0


def _index(reports) -> str:
    items = []
    for case, report in reports:
        color = "#34d399" if report.ok else "#f87171"
        verdict = "PASS" if report.ok else "FAIL"
        items.append(
            f"<li><a href='{case.slug}.html'>{case.title}</a> "
            f"<span style='color:{color};font-weight:700;font-size:12px'>{verdict}</span>"
            f"<div class='dim'>{case.blurb}</div></li>"
        )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>wallclock demo</title></head>
<body style="background:#0b1020;color:#e5e7eb;font:15px/1.6 ui-sans-serif,system-ui;padding:32px">
<h1>wallclock demo</h1>
<p class="dim" style="color:#94a3b8">Six schedules that look fine and break silently.</p>
<ol>{''.join(items)}</ol>
<p><a href="https://pypi.org/project/wallclock/" style="color:#60a5fa">wallclock</a></p>
</body></html>"""