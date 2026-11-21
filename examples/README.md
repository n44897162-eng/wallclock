# Examples

Each file is a complete `--config` input. The first run tells you whether the
schedule keeps its promises over the window; the second tells you how to fix it.

```bash
# a nightly job in a DST zone: will it always run?
wallclock check --config examples/nightly-backup.yaml

# a monthly report whose date is computed by the rule
wallclock check --config examples/monthly-report.yaml

# a CI gate: weekday jobs must stay inside working hours
wallclock check --config examples/ci-gate.yaml --fail-on warning
```

To see what a proposed change would do before merging it:

```bash
wallclock diff \
  --a-cron '30 2 * * *' --b-cron '30 3 * * *' --tz Europe/Berlin \
  --from 2026-01-01 --to 2027-01-01 --min-per-day 1 --fail-on regression
```

To see which regions a schedule is unsafe in:

```bash
wallclock scan --cron '30 2 * * *' --from 2026-01-01 --to 2027-01-01
```
