# Contributing

Thanks for taking the time to look at this. The project is deliberately small and
boring to work on: standard library only, one checker, no hidden state.

## Getting started

```bash
git clone <your-fork>
cd wallclock
python -m pip install -e '.[test]'
python -m pytest
```

`pytest` must stay green and reasonably fast (the whole suite runs in a few
seconds). If you add a test that needs a minute, it is probably testing too much.

## The shape of the code

| module | responsibility |
| --- | --- |
| `wallclock/tz.py` | offset transitions and the gap/overlap policies |
| `wallclock/cron.py` | Vixie cron parsing and wall-clock expansion |
| `wallclock/rrule.py` | RFC 5545 subset expansion |
| `wallclock/schedule.py` | rule + zone + policy -> firings (and dropped ones) |
| `wallclock/verify.py` | contracts, anomalies, exhaustive verification |
| `wallclock/diff.py` / `repair.py` | comparison and bounded repair search |
| `wallclock/report.py` / `cli.py` | text/JSON/HTML/ICS and the command line |

A change that touches semantics should usually come with three things:

1. a unit test for the new behaviour;
2. a **ground-truth or differential** test when the behaviour depends on tzdata,
   cron or RRULE subtleties (see `tests/test_rrule.py` and `tests/test_schedule.py`);
3. a line in `CHANGELOG.md`.

## Adding a contract

1. Subclass `Contract` in `wallclock/verify.py` with a `code`, `describe()` and
   `check(sim)`; every finding must carry the instants that prove it.
2. Teach `parse_contract()` the new key.
3. Add the flag to `_contracts_from()` and the `cflags` parser in `cli.py`.
4. Add it to the table in `README.md`.
5. Use `full_days()` / `_active_dates()` when the contract is calendar-based, so
   window edges and finite rules do not create false positives.

## Timezone data

Tests assert facts about the installed tz database (Samoa's skipped day, Lord
Howe's 30-minute DST, ...). Those facts are stable, but the CI workflow installs
`tzdata` explicitly. If you add a test for a future transition, prefer a year at
least two ahead of the release so the assertion does not age out of the rules.

## Style

`ruff` with the configuration in `pyproject.toml` (line length 110). Type hints on
public functions; comments explain *why*, not *what*.
