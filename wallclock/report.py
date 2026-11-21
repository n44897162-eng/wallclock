"""Rendering: text, JSON, a self-contained HTML timeline, and iCalendar export.

The HTML report is generated from the same numbers as the text one -- there is
no second analysis path.  The timeline is deliberately honest: runs are plotted
at their real instants, the band where wall-clock time did not exist is shaded,
and a dropped occurrence is an explicit red cross rather than a missing dot.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from .schedule import Firing, Schedule
from .tz import UTC, Transition, format_offset
from .verify import Report, Simulation, format_duration, full_days

_SEV_COLOR = {"error": "#f87171", "warning": "#fbbf24", "info": "#60a5fa"}
_KIND_COLOR = {
    "normal": "#34d399",
    "ambiguous_earlier": "#fbbf24",
    "ambiguous_later": "#fbbf24",
    "gap_forward": "#f59e0b",
    "gap_backward": "#f59e0b",
    "gap_clamp_end": "#f59e0b",
    "utc": "#a78bfa",
}


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------
def render_text(report: Report, max_findings: int = 40) -> str:
    sim = report.simulation
    lines: list[str] = []
    lines.append("wallclock - temporal contract verification")
    lines.append(f"schedule : {sim.schedule.describe()}")
    lines.append(
        f"window   : {_iso(sim.lo)} .. {_iso(sim.hi)} "
        f"({format_duration(sim.hi - sim.lo)}), tzdata {report.tzdata or 'unknown'}"
    )
    lines.append(
        f"runs     : {len(sim.firings)} in window, {len(sim.skipped)} wall-clock occurrence(s) skipped"
    )
    if sim.transitions:
        near = ", ".join(_describe_transition(sim, tr) for tr in sim.transitions[:4])
        more = f" (+{len(sim.transitions) - 4} more)" if len(sim.transitions) > 4 else ""
        lines.append(f"clocks   : {near}{more}")
    verdict = "PASS" if report.ok else "FAIL"
    lines.append(
        f"verdict  : {verdict} ({len(report.errors)} error(s), {len(report.warnings)} warning(s))"
    )
    if report.contracts:
        lines.append("contracts: " + "; ".join(c.describe() for c in report.contracts))
    lines.append("")
    if not report.findings:
        lines.append("no findings: every contract holds for every run in the window.")
        return "\n".join(lines)
    hint = "hint: `wallclock explain <code>` explains what a code means."
    shown = 0
    for sev in ("error", "warning", "info"):
        group = [f for f in report.findings if f.severity == sev]
        if not group:
            continue
        lines.append(f"--- {sev.upper()} ({len(group)}) ---")
        for f in group:
            if shown >= max_findings:
                lines.append(f"... {len(report.findings) - shown} more finding(s); use --format json for everything")
                return "\n".join(lines)
            when = f" [{_iso(f.at)}]" if f.at else ""
            lines.append(f"{f.code}{when}: {f.message}")
            shown += 1
        lines.append("")
    lines.append(hint)
    return "\n".join(lines)


def render_json(report: Report, indent: int = 2) -> str:
    return json.dumps(report.to_dict(), indent=indent, ensure_ascii=False)


def render_markdown(report: Report, max_findings: int = 50) -> str:
    """A compact report meant to be pasted into a pull request."""
    sim = report.simulation
    verdict = "PASS" if report.ok else "FAIL"
    lines = [
        f"## wallclock: {verdict}",
        "",
        f"`{sim.schedule.describe()}`",
        "",
        f"- window: `{_iso(sim.lo)}` -> `{_iso(sim.hi)}` (tzdata `{report.tzdata or 'unknown'}`)",
        f"- runs: {len(sim.firings)} | skipped: {len(sim.skipped)} | "
        f"errors: {len(report.errors)} | warnings: {len(report.warnings)}",
    ]
    if report.contracts:
        lines.append("- contracts: " + ", ".join(f"`{c.describe()}`" for c in report.contracts))
    lines += [
        "",
        "| severity | code | finding |",
        "| --- | --- | --- |",
    ]
    if report.findings:
        for finding in report.findings[:max_findings]:
            lines.append(f"| {finding.severity} | `{finding.code}` | {_md_cell(finding.message)} |")
        extra = len(report.findings) - max_findings
        if extra > 0:
            lines.append(f"| | | ... {extra} more; use `--format json` for everything |")
    else:
        lines.append("| - | - | no findings: every contract holds for every run in the window |")
    lines += ["", "_Exhaustive over the window. `wallclock explain <code>` explains a code._"]
    return "\n".join(lines)


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


# ---------------------------------------------------------------------------
# html
# ---------------------------------------------------------------------------
def render_html(report: Report, title: str = "wallclock report") -> str:
    sim = report.simulation
    verdict = "PASS" if report.ok else "FAIL"
    vcolor = "#34d399" if report.ok else "#f87171"
    panels = []
    transitions = sim.transitions[:4]
    if transitions:
        for tr in transitions:
            panels.append(_timeline_svg(sim, tr))
    else:
        panels.append(_overview_svg(sim))

    rows = []
    for f in report.findings[:200]:
        rows.append(
            "<tr>"
            f"<td><span class='sev' style='color:{_SEV_COLOR[f.severity]}'>{f.severity}</span></td>"
            f"<td><code>{html.escape(f.code)}</code></td>"
            f"<td>{html.escape(f.message)}</td>"
            f"<td class='dim'>{html.escape(_iso(f.at)) if f.at else ''}</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>severity</th><th>code</th><th>message</th><th>at (UTC)</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=4>no findings</td></tr>'}</tbody></table>"
    )

    contracts = "".join(f"<li><code>{html.escape(c.describe())}</code></li>" for c in report.contracts)
    counts = {
        "runs": len(sim.firings),
        "skipped": len(sim.skipped),
        "errors": len(report.errors),
        "warnings": len(report.warnings),
    }
    cards = "".join(
        f"<div class='card'><div class='k'>{k}</div><div class='v'>{v}</div></div>"
        for k, v in counts.items()
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
 :root {{ color-scheme: dark; }}
 body {{ margin:0; padding:32px; background:#0b1020; color:#e5e7eb;
        font:15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }}
 h1 {{ font-size:22px; margin:0 0 4px; letter-spacing:.2px; }}
 .sub {{ color:#94a3b8; margin-bottom:24px; }}
 .verdict {{ display:inline-block; padding:2px 10px; border-radius:999px; font-weight:700;
             background:{vcolor}22; color:{vcolor}; border:1px solid {vcolor}55; }}
 .grid {{ display:grid; grid-template-columns:repeat(4, minmax(120px, 1fr)); gap:12px; margin:20px 0 28px; }}
 .card {{ background:#111a2e; border:1px solid #1e293b; border-radius:12px; padding:12px 14px; }}
 .card .k {{ color:#94a3b8; font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
 .card .v {{ font-size:26px; font-weight:700; margin-top:2px; }}
 table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:14px; }}
 th {{ text-align:left; color:#94a3b8; font-weight:600; border-bottom:1px solid #1e293b; padding:8px; }}
 td {{ border-bottom:1px solid #16233a; padding:8px; vertical-align:top; }}
 .sev {{ font-weight:700; text-transform:uppercase; font-size:12px; }}
 .dim {{ color:#64748b; white-space:nowrap; }}
 code {{ background:#0f172a; border:1px solid #1e293b; border-radius:6px; padding:1px 5px; }}
 h2 {{ font-size:16px; margin:30px 0 8px; color:#cbd5e1; }}
 .panel {{ background:#0f172a; border:1px solid #1e293b; border-radius:14px; padding:10px 12px 4px; margin:14px 0; }}
 .legend {{ color:#94a3b8; font-size:12.5px; margin:6px 0 12px; }}
 footer {{ color:#64748b; font-size:12.5px; margin-top:28px; }}
</style></head><body>
<h1>wallclock <span class="verdict">{verdict}</span></h1>
<div class="sub">{html.escape(sim.schedule.describe())} &middot; {_iso(sim.lo)} &rarr; {_iso(sim.hi)}
 &middot; tzdata {report.tzdata or 'unknown'}</div>
<div class="grid">{cards}</div>
<h2>Contracts</h2>
<ul>{contracts or '<li class="dim">none specified; showing structural anomalies only</li>'}</ul>
<h2>Timeline</h2>
<div class="legend">green = normal run &middot; amber = run on a repeated or shifted wall-clock time
 &middot; red cross = occurrence that never happened &middot; red band = wall-clock time that did not exist
 &middot; amber band = wall-clock time that happened twice</div>
{''.join(panels)}
<h2>Findings</h2>
{table}
<footer>Generated by wallclock {_version()} &middot; exhaustive over the window: every recurrence was
resolved and every run checked &middot; verdict is only as good as the tzdata shown above.</footer>
</body></html>"""


def _timeline_svg(sim: Simulation, tr: Transition) -> str:
    zone = sim.zone
    width, height = 960, 142
    left, right, axis_y = 26, width - 26, 96
    start = tr.at - timedelta(hours=36)
    end = tr.at + timedelta(hours=36)
    span = (end - start).total_seconds()

    def x(dt: datetime) -> float:
        return left + (dt - start).total_seconds() / span * (right - left)

    parts: list[str] = []
    # bands
    if tr.is_gap:
        band = (tr.at, tr.at + abs(tr.gap))
    else:
        band = (tr.at - abs(tr.gap), tr.at + abs(tr.gap))
    color = "#f8717122" if tr.is_gap else "#fbbf2422"
    stroke = "#f8717166" if tr.is_gap else "#fbbf2466"
    band_w = max(x(band[1]) - x(band[0]), 4.0)
    parts.append(
        f"<rect x='{x(band[0]):.1f}' y='30' width='{band_w:.1f}' height='82' "
        f"fill='{color}' stroke='{stroke}' />"
    )
    # axis + ticks
    parts.append(f"<line x1='{left}' y1='{axis_y}' x2='{right}' y2='{axis_y}' stroke='#334155' />")
    tick = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=6 - start.hour % 6)
    while tick < end:
        xv = x(tick)
        parts.append(f"<line x1='{xv:.1f}' y1='{axis_y - 4}' x2='{xv:.1f}' y2='{axis_y + 4}' stroke='#475569' />")
        parts.append(
            f"<text x='{xv:.1f}' y='{axis_y + 18}' fill='#64748b' font-size='11' text-anchor='middle'>"
            f"{zone.wall(tick):%m-%d %H:%M}</text>"
        )
        tick += timedelta(hours=6)
    # runs
    for f in sim.firings:
        if not (start <= f.utc < end):
            continue
        color = _KIND_COLOR.get(f.kind, "#34d399")
        parts.append(
            f"<circle cx='{x(f.utc):.1f}' cy='72' r='4.4' fill='{color}'>"
            f"<title>{html.escape(f.utc.isoformat())} ({html.escape(f.kind)})</title></circle>"
        )
    # skipped occurrences
    for local in sim.skipped:
        res = zone.resolve(local, _policy_like(sim))
        if not res:
            continue
        at = res[0].utc
        if not (start <= at < end):
            continue
        xv, yv = x(at), 72
        parts.append(
            f"<g><line x1='{xv - 5:.1f}' y1='{yv - 5}' x2='{xv + 5:.1f}' y2='{yv + 5}' stroke='#ef4444' stroke-width='2.2'/>"
            f"<line x1='{xv - 5:.1f}' y1='{yv + 5}' x2='{xv + 5:.1f}' y2='{yv - 5}' stroke='#ef4444' stroke-width='2.2'/>"
            f"<title>skipped {html.escape(local.isoformat())}</title></g>"
        )
    title = (
        f"{zone.name}: clocks jump {format_offset(tr.before)} &rarr; {format_offset(tr.after)} "
        f"at {_iso(tr.at)}"
    )
    note = (
        "wall-clock time skipped" if tr.is_gap else "wall-clock time repeated"
    )
    return (
        "<div class='panel'>"
        f"<div style='font-weight:600;margin:4px 0 2px'>{title}</div>"
        f"<div class='dim' style='font-size:12px'>{note}; axis labels are local wall clock</div>"
        f"<svg viewBox='0 0 {width} {height}' width='100%' role='img' aria-label='{html.escape(title)}'>"
        f"{''.join(parts)}</svg></div>"
    )


def _overview_svg(sim: Simulation) -> str:
    zone = sim.zone
    width, height = 960, 142
    left, right, axis_y = 26, width - 26, 96
    start = sim.lo
    end = min(sim.hi, sim.lo + timedelta(days=7))
    span = max((end - start).total_seconds(), 1)
    parts = [f"<line x1='{left}' y1='{axis_y}' x2='{right}' y2='{axis_y}' stroke='#334155' />"]
    tick = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=24)
    while tick < end:
        xv = left + (tick - start).total_seconds() / span * (right - left)
        parts.append(f"<line x1='{xv:.1f}' y1='{axis_y - 4}' x2='{xv:.1f}' y2='{axis_y + 4}' stroke='#475569' />")
        parts.append(
            f"<text x='{xv:.1f}' y='{axis_y + 18}' fill='#64748b' font-size='11' text-anchor='middle'>"
            f"{zone.wall(tick):%m-%d}</text>"
        )
        tick += timedelta(days=1)
    for f in sim.firings:
        if not (start <= f.utc < end):
            continue
        xv = left + (f.utc - start).total_seconds() / span * (right - left)
        parts.append(
            f"<circle cx='{xv:.1f}' cy='74' r='4' fill='{_KIND_COLOR.get(f.kind, '#34d399')}'>"
            f"<title>{html.escape(f.utc.isoformat())}</title></circle>"
        )
    return (
        "<div class='panel'>"
        f"<div style='font-weight:600;margin:4px 0 2px'>{zone.name}: first 7 days</div>"
        "<div class='dim' style='font-size:12px'>no offset changes inside the window</div>"
        f"<svg viewBox='0 0 {width} {height}' width='100%' role='img'>"
        f"{''.join(parts)}</svg></div>"
    )


# ---------------------------------------------------------------------------
# icalendar
# ---------------------------------------------------------------------------
def render_ics(
    firings: Sequence[Firing],
    schedule: Schedule | None = None,
    name: str = "wallclock",
    limit: int = 1000,
    now: datetime | None = None,
) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    label = name
    if schedule is not None:
        label = f"{name}: {schedule.describe()}"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//wallclock//temporal contract verification//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_text(label)}",
    ]
    for f in firings[:limit]:
        uid = f"wallclock-{f.utc.strftime('%Y%m%dT%H%M%SZ')}-{f.sequence}@wallclock"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{f.utc.strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{_ics_text(label)}",
            f"DESCRIPTION:{_ics_text(f'kind={f.kind}; intended {f.local.isoformat()}')}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _ics_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _describe_transition(sim: Simulation, tr: Transition) -> str:
    local = sim.zone.wall(tr.at)
    return (
        f"{local:%Y-%m-%d %H:%M} local {format_offset(tr.before)}->{format_offset(tr.after)}"
        f" ({_iso(tr.at)})"
    )


def _policy_like(sim: Simulation):
    from .tz import Policy

    return Policy(gap="shift_forward", ambiguous=sim.schedule.policy.ambiguous)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _version() -> str:
    from . import __version__

    return __version__