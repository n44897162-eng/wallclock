# The model

This is the precise version of what `wallclock` claims. It is short on purpose:
the interesting part is what the claim does *not* cover.

## 1. Wall clock and zone

Let `L` be the set of naive local datetimes (a civil clock) and `U` the set of
UTC instants. A zone `z` provides a partial map

```
at_z : L ⇀ U
```

that is neither total nor injective:

* `|at_z(l)| = 0` when `l` falls in a **gap** (offset increased; the clock jumped
  forward),
* `|at_z(l)| = 2` when `l` falls in an **overlap** (offset decreased; the clock
  repeated),
* `|at_z(l)| = 1` otherwise.

`wallclock.tz.Zone` computes this exactly, from tzdata, by round-tripping the
instant (`u -> wall_z(u) == l`). Python's `zoneinfo` exposes `fold` but resolves
the two impossible cases silently; we never use the silent behaviour.

A **policy** turns the partial map into a total one, per case:

| case | policy | result |
| --- | --- | --- |
| gap | `skip` | no instant (the occurrence is lost) |
| gap | `shift_forward` | `l - offset_before` (keeps the intra-gap offset: 02:30 → 03:30) |
| gap | `shift_backward` | `l - offset_after` (02:30 → 01:30) |
| gap | `clamp_end` | the transition instant (02:30 → 03:00) |
| overlap | `earlier` / `later` | the first / second instant |
| overlap | `both` | both instants |

These are the policies real systems implement; naming them is what lets the
verifier say "this contract holds if you switch policy" instead of guessing.

## 2. Schedule

A schedule is `(R, z, π, a)` where

* `R` is a rule — a 5-field cron expression or an RRULE + `DTSTART`, interpreted
  as a set of naive local datetimes;
* `z` is a zone;
* `π` is a resolution policy;
* `a ∈ {local, utc}` is the **anchor**: with `a = utc` the rule is iterated on the
  UTC clock and then rendered locally, which is a different schedule.

Its **firing sequence** over a window `W = [lo, hi) ⊂ U` is

```
F(W) = sort { u ∈ U : ∃ l ∈ R, u ∈ at_z^π(l), lo ≤ u < hi }
```

`Schedule.expand` additionally yields the recurrence ids whose `at_z^π` is empty
(the dropped occurrences), because the verifier needs them and `fire` hides them.

## 3. Contracts

A contract is a predicate on `F(W)` and the local calendar it lands in:

```
max_gap(d)      ∀ consecutive u_i, u_{i+1} ∈ F(W).  u_{i+1} - u_i ≤ d
local_window(s,e)  ∀ u ∈ F(W).  s ≤ time_of_day(wall_z(u)) < e
min_per_day(n)  ∀ fully covered local dates D.  |{u ∈ F(W) : wall_z(u) ∈ D}| ≥ n
max_per_day(n)  ∀ local dates D.  |{u ∈ F(W) : wall_z(u) ∈ D}| ≤ n
max_drift(d)    ∀ u ∈ F(W).  |wall_z(u) - requested_local(u)| ≤ d
```

"Fully covered" means the local date's UTC span is contained in `W`; partial
dates at the window edges are excluded so that the contract cannot fail for a
reason that is an artefact of where you cut the window.

## 4. The guarantee

`verify(s, contracts, lo, hi)` enumerates `R ∩ (local image of W)` completely,
resolves every element, and evaluates every contract against every firing.

> **Theorem (exhaustiveness).** If `verify` returns no `error` finding, then no
> contract is violated by any firing in `W`, for the given `R`, `z`, `π`, and the
> installed tzdata.

This is a statement about a finite set, so it is exhaustive rather than
statistical. Its *trust assumptions* are explicit:

1. **tzdata** is correct for `W`. The tz database contains rules computed into
   the future (and a TZ string beyond them); a 20-year horizon is a projection
   made by the database, not a fact.
2. **The parsers** implement the syntax they claim. This is not taken on faith:
   RRULE expansion is differentially tested against `dateutil` on 250 random
   rules, cron against `croniter` on 150, and both against a minute-by-minute
   brute-force simulator (see `tests/`).
3. **The policy matches your scheduler.** `wallclock` reports which policy it
   used and can re-verify under another one; it cannot know what your daemon
   does. (Two real daemons disagree about `*/n` in the day fields — the test
   suite pins that divergence.)
4. **No external effects.** Catch-up jobs, retries, backfills, manual runs, and
   leap seconds are outside the model.

The last point is where a "PASS" can still be wrong in production: a scheduler
that fires missed jobs late has different staleness than one that skips them.
That is a modelling choice, not a detail, and it is why `gap` is an input.

## 5. Why `max_gap` is the interesting contract

`min_per_day` reads naturally but is a calendar statement. What most systems
actually promise is a *freshness bound*: "data is never more than 26 hours old",
"the report is always generated before 09:00". `max_gap` is that promise in
absolute time, and it is exactly the property that a skipped occurrence breaks
in the most surprising way: one missing daily run is not a 24-hour gap, it is a
**47-hour** gap, because the following run is still anchored to its own wall-clock
time. No dashboard shows this, because nothing fired to be late.

## 6. What a witness contains

Every finding carries the instants that establish it (the two runs of a gap, the
transition instant, the requested vs effective local time, the drift in
seconds). The HTML report plots them on a 72-hour axis around the transition, so
the claim is visually checkable rather than trusted.