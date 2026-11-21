"""Report renderers and CLI tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from wallclock.cli import main
from wallclock.report import render_html, render_ics, render_json, render_text
from wallclock.schedule import Schedule
from wallclock.verify import horizon, verify

UTC = timezone.utc


def sample_report():
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin")
    lo, hi = horizon("Europe/Berlin", "2026-03-01", "2026-04-01")
    return verify(s, [{"max_gap": "26h"}, {"min_per_day": 1}], lo, hi)


def test_text_report_contains_the_story() -> None:
    text = render_text(sample_report())
    assert "FAIL" in text
    assert "Europe/Berlin" in text
    assert "does not exist" in text
    assert "min_per_day" in text
    assert "tzdata" in text


def test_text_report_caps_findings() -> None:
    s = Schedule.from_cron("30 2 * * *", "Europe/Berlin", anchor="utc")
    lo, hi = horizon("Europe/Berlin", "2026-01-01", "2027-01-01")
    text = render_text(verify(s, [{"local_window": "08:00-09:00"}], lo, hi), max_findings=5)
    assert "more finding" in text


def test_json_report_round_trips() -> None:
    payload = json.loads(render_json(sample_report()))
    assert payload["ok"] is False
    assert payload["counts"]["skipped"] == 1
    assert payload["findings"][0]["severity"] in ("error", "warning", "info")


def test_html_report_is_self_contained() -> None:
    html = render_html(sample_report())
    assert html.startswith("<!doctype html>")
    assert "<svg" in html and "wall-clock time skipped" in html
    assert "<script" not in html  # no JS, no external assets


def test_html_escapes_user_text() -> None:
    s = Schedule.from_rrule("FREQ=DAILY", "2026-01-05T09:00", "UTC")
    lo, hi = horizon("UTC", "2026-01-01", "2026-01-10")
    html = render_html(verify(s, [], lo, hi))
    assert "<script>" not in html


def test_ics_export() -> None:
    s = Schedule.from_cron("0 12 * * *", "Europe/Berlin")
    lo, hi = horizon("Europe/Berlin", "2026-03-01", "2026-03-05")
    firings = s.fire(lo, hi)
    ics = render_ics(firings, s, name="test", now=datetime(2026, 1, 1, tzinfo=UTC))
    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert ics.count("BEGIN:VEVENT") == len(firings)
    assert "DTSTART:20260301T110000Z" in ics  # 12:00 Berlin = 11:00Z in winter
    assert ics.endswith("END:VCALENDAR\r\n")


def test_ics_escapes_text() -> None:
    s = Schedule.from_cron("0 12 * * *", "UTC")
    lo, hi = horizon("UTC", "2026-03-01", "2026-03-02")
    ics = render_ics(s.fire(lo, hi), s, name="a,b;c")
    assert "a\\,b\\;c" in ics


# -- CLI -------------------------------------------------------------------
def test_cli_check_pass(capsys) -> None:
    code = main(["--" if False else "check", "--cron", "0 12 * * *", "--tz", "UTC",
                 "--from", "2026-01-01", "--to", "2026-02-01", "--min-per-day", "1"])
    assert code == 0
    assert "PASS" in capsys.readouterr().out


def test_cli_check_fail_exit_code(capsys) -> None:
    code = main(["check", "--cron", "30 2 * * *", "--tz", "Europe/Berlin",
                 "--from", "2026-03-01", "--to", "2026-04-01", "--min-per-day", "1"])
    assert code == 1
    assert "FAIL" in capsys.readouterr().out


def test_cli_check_json(capsys) -> None:
    code = main(["check", "--cron", "0 12 * * *", "--tz", "UTC",
                 "--from", "2026-01-01", "--to", "2026-02-01", "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def test_cli_check_html_out(tmp_path) -> None:
    target = tmp_path / "report.html"
    code = main(["check", "--cron", "0 12 * * *", "--tz", "UTC",
                 "--from", "2026-01-01", "--to", "2026-02-01",
                 "--format", "html", "--out", str(target)])
    assert code == 0
    assert "<svg" in target.read_text()


def test_cli_sim_and_ics(tmp_path, capsys) -> None:
    ics = tmp_path / "runs.ics"
    code = main(["sim", "--cron", "0 12 * * *", "--tz", "UTC",
                 "--from", "2026-01-01", "--to", "2026-01-05", "--ics", str(ics)])
    assert code == 0
    assert "runs    : 4" in capsys.readouterr().out
    assert ics.exists() and ics.read_text().startswith("BEGIN:VCALENDAR")


def test_cli_diff(capsys) -> None:
    code = main(["diff", "--a-cron", "30 2 * * *", "--b-cron", "30 3 * * *",
                 "--tz", "Europe/Berlin", "--from", "2026-07-01", "--to", "2026-08-01"])
    assert code == 0
    out = capsys.readouterr().out
    assert "moved" in out and "31" in out


def test_cli_repair(capsys) -> None:
    code = main(["repair", "--cron", "30 2 * * *", "--tz", "Europe/Berlin",
                 "--from", "2026-03-01", "--to", "2026-04-01", "--min-per-day", "1"])
    assert code == 1
    out = capsys.readouterr().out
    assert "verified repairs" in out


def test_cli_zones(capsys) -> None:
    code = main(["zones", "--zone", "Europe/Berlin", "--from", "2026-01-01", "--to", "2027-01-01"])
    assert code == 0
    assert "Europe/Berlin: 2 offset change(s)" in capsys.readouterr().out


def test_cli_zones_list(capsys) -> None:
    assert main(["zones", "--list"]) == 0
    assert "Europe/Berlin" in capsys.readouterr().out


def test_cli_config_file(tmp_path, capsys) -> None:
    cfg = tmp_path / "spec.yaml"
    cfg.write_text(
        """
schedule:
  cron: "30 2 * * *"
  timezone: Europe/Berlin
contracts:
  - min_per_day: 1
window:
  from: 2026-03-01
  to: 2026-04-01
""".strip()
    )
    code = main(["check", "--config", str(cfg)])
    assert code == 1
    assert "2026-03-29" in capsys.readouterr().out


def test_cli_spec_json(capsys) -> None:
    spec = json.dumps({"schedule": {"cron": "0 12 * * *", "timezone": "UTC"}})
    code = main(["check", "--spec", spec, "--from", "2026-01-01", "--to", "2026-02-01",
                 "--min-per-day", "1"])
    assert code == 0


def test_cli_bad_input_exits_2(capsys) -> None:
    assert main(["check", "--cron", "not a cron", "--tz", "UTC"]) == 2
    assert "wallclock:" in capsys.readouterr().err
    assert main(["check", "--cron", "* * * * *", "--tz", "Mars/Base"]) == 2
    assert main(["sim", "--cron", "* * * * *", "--rrule", "FREQ=DAILY"]) == 2


def test_cli_demo_runs(capsys) -> None:
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "wallclock demo" in out and "Berlin" in out


def test_cli_demo_html(tmp_path, capsys) -> None:
    assert main(["demo", "--out", str(tmp_path)]) == 0
    index = tmp_path / "index.html"
    assert index.exists()
    assert len(list(tmp_path.glob("*.html"))) >= 7

def test_cli_scan(capsys) -> None:
    code = main(["scan", "--cron", "30 2 * * *", "--from", "2026-03-01", "--to", "2026-04-01"])
    assert code == 0
    out = capsys.readouterr().out
    assert "scanned" in out and "skip at least one occurrence" in out
    assert "Europe/Berlin" in out
    assert "America/Santiago" not in out  # Chile's transition is at midnight, not 02:30


def test_cli_scan_json(capsys) -> None:
    code = main(["scan", "--cron", "0 0 * * *", "--from", "2026-08-15", "--to", "2026-10-01",
                 "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(z["zone"] == "America/Santiago" for z in payload["zones"])
