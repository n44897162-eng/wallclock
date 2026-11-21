"""Benchmark wallclock: how long does an exhaustive verification take?

Run: python3 scripts/bench.py
"""

from __future__ import annotations

import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from datetime import datetime, timedelta, timezone

from wallclock import Schedule, horizon, verify
from wallclock.tz import list_zones, tzdata_version

UTC = timezone.utc

ZONES = [
    "UTC", "Europe/Berlin", "Europe/Dublin", "Europe/London", "America/New_York",
    "America/Santiago", "America/Sao_Paulo", "Australia/Lord_Howe", "Australia/Sydney",
    "Asia/Kathmandu", "Asia/Tehran", "Pacific/Apia", "Pacific/Chatham", "Africa/Casablanca",
    "Antarctica/Troll", "America/St_Johns",
]
CONTRACTS = [{"min_per_day": 1}, {"max_gap": "26h"}]


def run_zone(zone: str, years: int = 1) -> tuple[float, int, int, int]:
    lo = horizon(zone, "2026-01-01", f"{2026 + years}-01-01")[0]
    hi = horizon(zone, "2026-01-01", f"{2026 + years}-01-01")[1]
    schedule = Schedule.from_cron("30 2 * * *", zone)
    t0 = time.perf_counter()
    report = verify(schedule, CONTRACTS, lo, hi)
    dt = time.perf_counter() - t0
    return dt, len(report.simulation.firings), len(report.simulation.skipped), len(report.errors)


def main() -> None:
    print(f"tzdata {tzdata_version()} | python wallclock benchmark")
    print()
    print("| zone | window | runs | skipped | errors | time |")
    print("| --- | --- | ---: | ---: | ---: | ---: |")
    times = []
    for zone in ZONES:
        dt, runs, skipped, errors = run_zone(zone)
        times.append(dt)
        print(f"| {zone} | 1y | {runs} | {skipped} | {errors} | {dt * 1000:.0f} ms |")
    print()
    print(f"1y x {len(ZONES)} zones: total {sum(times):.2f}s, median {statistics.median(times) * 1000:.0f} ms")

    dt, runs, skipped, errors = run_zone("Europe/Berlin", years=10)
    print(f"10y daily Berlin: {runs} runs, {skipped} skipped, {errors} errors in {dt * 1000:.0f} ms")

    lo, hi = horizon("Europe/Berlin", "2026-01-01", "2027-01-01")
    hourly = Schedule.from_rrule("FREQ=HOURLY", "2026-01-01T00:00", "Europe/Berlin")
    t0 = time.perf_counter()
    report = verify(hourly, [{"max_gap": "2h"}], lo, hi)
    dt = time.perf_counter() - t0
    print(f"1y hourly RRULE Berlin: {len(report.simulation.firings)} runs in {dt * 1000:.0f} ms")

    all_zones = list_zones()
    t0 = time.perf_counter()
    total = 0
    for name in all_zones:
        report = verify(Schedule.from_cron("30 2 * * *", name), [], lo, hi)
        total += len(report.simulation.firings)
    dt = time.perf_counter() - t0
    print(f"all {len(all_zones)} zones x 1y, no contracts: {total} runs in {dt:.2f}s "
          f"({total / dt:,.0f} runs/s)")


if __name__ == "__main__":
    main()
