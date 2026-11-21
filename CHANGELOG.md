# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project aims at
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-11-21

### Added

- **New contracts**: `min_per_week` / `max_per_week` (ISO weeks; partial weeks at
  a window edge or outside a finite rule are not judged) and `forbidden_window`
  (no run may land inside a local time range; the range may cross midnight).
- **Contract-aware `diff`**: the same checker judges both sides, so a change is
  reported as `improved` / `regressed` / `unchanged-pass` / `both-fail` instead
  of only as a list of moved runs. `--fail-on regression|error` makes it a CI gate.
- **`--fail-on` for `check`**: `error` (default), `warning`, or `never`.
- **`scan` accepts contracts** and ranks zones by contract violations, showing
  the first witness per zone; `--fail-on error` for CI.
- **`wallclock explain <code>`**: one entry per contract and finding, with a
  runnable example; `--list` and `--format json` for tooling.
- **`--format markdown`** for `check`: a compact table meant to be pasted into a
  pull request.
- `demo` writes an `.ics` of the flagship case next to the HTML pages.

### Changed

- Dropped occurrences are now grouped per DST gap: one finding with a count
  (`60 consecutive occurrences disappeared`) instead of 60 identical findings.
- Windows that would produce more than 1.5M runs are refused with a clear error
  instead of silently consuming memory.

### Fixed

- `min_per_day` no longer flags local dates before `DTSTART` or after a finite
  rule's `UNTIL`/`COUNT` was exhausted.

## [0.1.0] - 2026-11-21

- Initial release: exact timezone resolution, Vixie cron, RFC 5545 RRULE subset,
  exhaustive contract verification (`max_gap`, `local_window`, per-day counts,
  `max_drift`), structural anomalies with witnesses, semantic diff, verified
  repair synthesis, text/JSON/HTML reports, `.ics` export, zone scan.
