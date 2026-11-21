"""Command line interface.

Subcommands:

    wallclock check    schedule + contracts -> verdict (text / json / html)
    wallclock sim      schedule -> the actual firing list (optionally .ics)
    wallclock diff     two schedules -> what changes over a window
    wallclock repair   schedule + contracts -> verified fixes
    wallclock zones    offset transitions for a zone
    wallclock demo     the built-in tour of surprising cases

Exit codes: 0 = contracts hold, 1 = a contract is violated, 2 = bad input.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import __version__
from .diff import diff as diff_schedules
from .errors import WallclockError
from .repair import repair as repair_schedule
from .report import render_html, render_ics, render_json, render_text
from .schedule import Schedule
from .tz import AMBIGUOUS_POLICIES, GAP_POLICIES, UTC, Policy, format_offset, get_zone, list_zones
from .verify import (
    format_duration,
    horizon,
    parse_contract,
    parse_duration,
    verify,
)

CONFIG_SUFFIXES = (".yaml", ".yml", ".json")


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wallclock",
        description="Temporal-contract verification for scheduled jobs: exact wall-clock "
        "simulation, exhaustive proofs over a window, counterexample witnesses, repair.",
    )
    p.add_argument("--version", action="version", version=f"wallclock {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sch = argparse.ArgumentParser(add_help=False)
    sch.add_argument("--cron", help="5-field cron expression")
    sch.add_argument("--rrule", help="RFC 5545 RRULE (strict subset)")
    sch.add_argument("--dtstart", help="RRULE anchor, ISO 8601 wall clock")
    sch.add_argument("--tz", default="UTC", help="IANA timezone (default UTC)")
    sch.add_argument("--gap", choices=GAP_POLICIES, default="skip",
                     help="what to do when the wall-clock time does not exist")
    sch.add_argument("--ambiguous", choices=AMBIGUOUS_POLICIES, default="earlier",
                     help="what to do when the wall-clock time exists twice")
    sch.add_argument("--anchor", choices=("local", "utc"), default="local",
                     help="anchor the recurrence in local time or in UTC")

    win = argparse.ArgumentParser(add_help=False)
    win.add_argument("--from", dest="from_", help="window start (date or ISO datetime, local to --tz)")
    win.add_argument("--to", dest="to", help="window end (date or ISO datetime)")
    win.add_argument("--months", type=int, default=12, help="window length when --to is omitted")

    con = argparse.ArgumentParser(add_help=False)
    con.add_argument("--max-gap", help="e.g. 26h")
    con.add_argument("--local-window", help="e.g. 08:00-10:00")
    con.add_argument("--min-per-day", type=int)
    con.add_argument("--max-per-day", type=int)
    con.add_argument("--max-drift", help="e.g. 0s")
    con.add_argument("--contract", action="append", default=[],
                     help="extra contract as key=value (repeatable)")
    con.add_argument("--config", help="YAML/JSON file with schedule/contracts/window")
    con.add_argument("--spec", help="inline JSON with schedule/contracts/window")

    out = argparse.ArgumentParser(add_help=False)
    out.add_argument("--format", choices=("text", "json", "html"), default="text")
    out.add_argument("--out", help="write the report to a file instead of stdout")
    out.add_argument("--ics", help="also write the firings as an .ics calendar")

    c = sub.add_parser("check", parents=[sch, win, con, out], help="verify contracts over a window")
    c.add_argument("--repair", action="store_true", help="also propose verified fixes")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("sim", parents=[sch, win, out], help="list the actual runs")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_sim)

    d = sub.add_parser("diff", parents=[win], help="semantic diff of two schedules")
    d.add_argument("--a-cron")
    d.add_argument("--a-rrule")
    d.add_argument("--a-dtstart")
    d.add_argument("--b-cron")
    d.add_argument("--b-rrule")
    d.add_argument("--b-dtstart")
    d.add_argument("--tz", default="UTC")
    d.add_argument("--gap", choices=GAP_POLICIES, default="skip")
    d.add_argument("--ambiguous", choices=AMBIGUOUS_POLICIES, default="earlier")
    d.add_argument("--tolerance", default="3h", help="pairing window for 'moved' runs")
    d.add_argument("--format", choices=("text", "json"), default="text")
    d.add_argument("--out")
    d.set_defaults(func=cmd_diff)

    r = sub.add_parser("repair", parents=[sch, win, con], help="synthesize verified fixes")
    r.add_argument("--format", choices=("text", "json"), default="text")
    r.add_argument("--out")
    r.set_defaults(func=cmd_repair)

    z = sub.add_parser("zones", help="offset transitions for a zone")
    z.add_argument("--list", action="store_true", help="list known zone names")
    z.add_argument("--zone", default="UTC")
    z.add_argument("--from", dest="from_")
    z.add_argument("--to")
    z.add_argument("--months", type=int, default=12)
    z.add_argument("--out")
    z.set_defaults(func=cmd_zones)

    dm = sub.add_parser("demo", help="run the built-in tour")
    dm.add_argument("--out", help="directory for the HTML tour (index.html + one page per case)")
    dm.add_argument("--json", action="store_true", help="emit the tour as JSON")
    dm.set_defaults(func=cmd_demo)
    return p


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_check(args: argparse.Namespace) -> int:
    config = _load_config(args)
    schedule = _schedule_from(config.get("schedule"), args)
    lo, hi = _window(args, schedule.zone.name, config.get("window"))
    contracts = _contracts_from(args, config.get("contracts"))
    report = verify(schedule, contracts, lo, hi)
    _emit_report(report, args)
    if getattr(args, "repair", False) and not report.ok:
        fixes = repair_schedule(schedule, contracts, lo, hi)
        _print_repairs(fixes)
    return 0 if report.ok else 1


def cmd_sim(args: argparse.Namespace) -> int:
    schedule = _schedule_from(None, args)
    lo, hi = _window(args, schedule.zone.name, None)
    firings = schedule.fire(lo, hi)
    if args.ics:
        Path(args.ics).write_text(render_ics(firings, schedule, name="wallclock", limit=5000), encoding="utf-8")
    if args.format == "json":
        payload = {
            "schedule": schedule.describe(),
            "window": {"from": lo.isoformat(), "to": hi.isoformat()},
            "count": len(firings),
            "firings": [
                {"utc": f.utc.isoformat(), "local": f.effective_local.isoformat(),
                 "kind": f.kind, "drift_seconds": int(f.drift.total_seconds())}
                for f in firings[: args.limit]
            ],
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False)
    else:
        lines = [
            f"schedule: {schedule.describe()}",
            f"window  : {_iso(lo)} .. {_iso(hi)}",
            f"runs    : {len(firings)}",
            "",
        ]
        for f in firings[: args.limit]:
            mark = "" if f.kind == "normal" else f"  [{f.kind}]"
            lines.append(f"{_iso(f.utc):20s}  {f.effective_local:%Y-%m-%d %H:%M} local{mark}")
        if len(firings) > args.limit:
            lines.append(f"... {len(firings) - args.limit} more (use --limit)")
        text = "\n".join(lines)
    _write(text + ("\n" if not text.endswith("\n") else ""), args.out)
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    policy = Policy(gap=args.gap, ambiguous=args.ambiguous)
    a = _build_schedule(args.a_cron, args.a_rrule, args.a_dtstart, args.tz, policy, "local", "a")
    b = _build_schedule(args.b_cron, args.b_rrule, args.b_dtstart, args.tz, policy, "local", "b")
    lo, hi = _window(args, args.tz, None)
    result = diff_schedules(a, b, lo, hi, tolerance=parse_duration(args.tolerance))
    if args.format == "json":
        text = json.dumps(result.to_dict(), indent=2, ensure_ascii=False)
    else:
        lines = [
            f"a: {a.describe()}",
            f"b: {b.describe()}",
            f"window: {_iso(lo)} .. {_iso(hi)}",
            "",
            result.summary(),
        ]
        for x, y in result.moved[:10]:
            lines.append(f"  moved  {_iso(x.utc)} -> {_iso(y.utc)}  ({format_duration(y.utc - x.utc)})")
        for f in result.added[:10]:
            lines.append(f"  added  {_iso(f.utc)}  ({f.effective_local:%Y-%m-%d %H:%M} local)")
        for f in result.removed[:10]:
            lines.append(f"  removed {_iso(f.utc)}")
        text = "\n".join(lines)
    _write(text + "\n", args.out)
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    config = _load_config(args)
    schedule = _schedule_from(config.get("schedule"), args)
    lo, hi = _window(args, schedule.zone.name, config.get("window"))
    contracts = _contracts_from(args, config.get("contracts"))
    base = verify(schedule, contracts, lo, hi)
    fixes = repair_schedule(schedule, contracts, lo, hi)
    if args.format == "json":
        text = json.dumps(
            {
                "original": base.to_dict(),
                "candidates": [
                    {"description": f.description, "cost": f.cost, "ok": f.ok,
                     "errors": f.errors, "schedule": f.schedule.to_dict()}
                    for f in fixes
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        _write(text + "\n", args.out)
        return 0 if base.ok else 1
    _emit_report(base, args)
    _print_repairs(fixes)
    return 0 if base.ok else 1


def cmd_zones(args: argparse.Namespace) -> int:
    if args.list:
        names = list_zones()
        _write("\n".join(names) + "\n", args.out)
        return 0
    zone = get_zone(args.zone)
    lo, hi = _window(args, args.zone, None)
    transitions = zone.transitions(lo, hi)
    lines = [f"{zone.name}: {len(transitions)} offset change(s)", ""]
    for tr in transitions:
        lines.append(
            f"  {_iso(tr.at)}  local {zone.wall(tr.at):%Y-%m-%d %H:%M}  "
            f"{format_offset(tr.before)} -> {format_offset(tr.after)}  "
            f"({format_duration(tr.gap)} gap/repeat)"
        )
    _write("\n".join(lines) + "\n", args.out)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    return run_demo(out_dir=args.out, as_json=args.json)


# ---------------------------------------------------------------------------
# helpers: input
# ---------------------------------------------------------------------------
def _load_config(args: argparse.Namespace) -> dict:
    if getattr(args, "spec", None):
        try:
            return json.loads(args.spec)
        except json.JSONDecodeError as exc:
            raise WallclockError(f"--spec is not valid JSON: {exc}") from None
    path = getattr(args, "config", None)
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise WallclockError(f"config file not found: {path}")
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(text)
    except ImportError:
        raise WallclockError("YAML config needs PyYAML; use a .json config instead") from None
    return loaded or {}


def _schedule_from(config_spec: dict | None, args: argparse.Namespace) -> Schedule:
    if config_spec:
        schedule = Schedule.from_dict(config_spec)
        # explicit flags still win over the file
        if getattr(args, "cron", None) or getattr(args, "rrule", None):
            schedule = _build_schedule(args.cron, args.rrule, args.dtstart, args.tz,
                                       _policy(args), args.anchor, "schedule")
        return schedule
    return _build_schedule(args.cron, args.rrule, args.dtstart, args.tz, _policy(args), args.anchor, "schedule")


def _build_schedule(cron, rrule, dtstart, tz, policy, anchor, label) -> Schedule:
    if bool(cron) == bool(rrule):
        raise WallclockError(f"{label}: give exactly one of --cron or --rrule")
    if cron:
        return Schedule.from_cron(cron, tz, policy=policy, anchor=anchor)
    assert rrule is not None
    if not dtstart:
        raise WallclockError(f"{label}: --rrule needs --dtstart")
    return Schedule.from_rrule(rrule, dtstart, tz, policy=policy, anchor=anchor)


def _policy(args: argparse.Namespace) -> Policy:
    return Policy(gap=getattr(args, "gap", "skip"), ambiguous=getattr(args, "ambiguous", "earlier"))


def _window(args: argparse.Namespace, zone: str, config_window: dict | None) -> tuple[datetime, datetime]:
    start = getattr(args, "from_", None) or (config_window or {}).get("from")
    end = getattr(args, "to", None) or (config_window or {}).get("to")
    months = getattr(args, "months", 12) or 12
    start = _text(start) if start else get_zone(zone).today().isoformat()
    if not end:
        end = _add_months(date.fromisoformat(start[:10]), months).isoformat()
    return horizon(zone, start, _text(end))


def _text(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _add_months(day: date, months: int) -> date:
    year = day.year + (day.month - 1 + months) // 12
    month = (day.month - 1 + months) % 12 + 1
    # clamp to the last valid day of the target month
    while True:
        try:
            return date(year, month, day.day)
        except ValueError:
            day = day.replace(day=day.day - 1)


def _contracts_from(args: argparse.Namespace, config_contracts) -> list:
    out: list = []
    if config_contracts:
        out.extend(config_contracts)
    if getattr(args, "max_gap", None):
        out.append({"max_gap": args.max_gap})
    if getattr(args, "local_window", None):
        out.append({"local_window": args.local_window})
    if getattr(args, "min_per_day", None) is not None:
        out.append({"min_per_day": args.min_per_day})
    if getattr(args, "max_per_day", None) is not None:
        out.append({"max_per_day": args.max_per_day})
    if getattr(args, "max_drift", None):
        out.append({"max_drift": args.max_drift})
    for raw in getattr(args, "contract", []) or []:
        if "=" not in raw:
            raise WallclockError(f"--contract needs key=value, got {raw!r}")
        key, value = raw.split("=", 1)
        out.append({key.strip(): value.strip()})
    return [parse_contract(c) if isinstance(c, dict) else c for c in out]


# ---------------------------------------------------------------------------
# helpers: output
# ---------------------------------------------------------------------------
def _emit_report(report, args: argparse.Namespace) -> None:
    if args.format == "json":
        text = render_json(report)
    elif args.format == "html":
        text = render_html(report)
    else:
        text = render_text(report)
    _write(text + "\n", args.out)
    if getattr(args, "ics", None):
        Path(args.ics).write_text(render_ics(report.simulation.firings, report.schedule), encoding="utf-8")


def _print_repairs(fixes) -> None:
    passing = [f for f in fixes if f.ok]
    print()
    if not passing:
        print("no repair found inside the search space; the closest candidates still fail:")
        for f in fixes[:3]:
            print(f"  [{f.cost}] {f.description} -> {f.errors} error(s)")
        return
    print(f"verified repairs ({len(passing)}):")
    for f in passing[:5]:
        print(f"  [{f.cost}] {f.description}")
        print(f"        -> {f.schedule.describe()}")


def _write(text: str, path: str | None) -> None:
    if path:
        Path(path).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except WallclockError as exc:
        print(f"wallclock: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"wallclock: bad JSON: {exc}", file=sys.stderr)
        return 2