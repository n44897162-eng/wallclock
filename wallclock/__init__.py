"""wallclock -- temporal-contract verification for scheduled jobs.

The package is deliberately dependency-free (standard library only).  See
``README.md`` for the model: a schedule is a *wall-clock* intent, a zone turns
it into instants, and a contract is a predicate over the resulting firing
sequence.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .errors import HorizonError, SpecError, WallclockError, ZoneError
from .tz import (
    AMBIGUOUS_POLICIES,
    DEFAULT_POLICY,
    GAP_POLICIES,
    Policy,
    Resolution,
    Transition,
    Zone,
    format_offset,
    get_zone,
    list_zones,
    tzdata_version,
)

from .cron import Cron, parse_cron
from .diff import Diff, diff
from .repair import Repair, repair
from .rrule import RRule, parse_rrule
from .schedule import Firing, Schedule
from .verify import (
    Contract,
    Finding,
    Report,
    detect_anomalies,
    format_duration,
    horizon,
    parse_contract,
    parse_duration,
    simulate,
    verify,
)

__all__ = [
    "__version__",
    "AMBIGUOUS_POLICIES",
    "DEFAULT_POLICY",
    "GAP_POLICIES",
    "HorizonError",
    "Policy",
    "Resolution",
    "SpecError",
    "Transition",
    "WallclockError",
    "Zone",
    "ZoneError",
    "format_offset",
    "get_zone",
    "list_zones",
    "tzdata_version",
    # high-level API
    "Cron",
    "Diff",
    "Firing",
    "Finding",
    "RRule",
    "Repair",
    "Report",
    "Schedule",
    "Contract",
    "diff",
    "detect_anomalies",
    "format_duration",
    "horizon",
    "parse_contract",
    "parse_cron",
    "parse_duration",
    "parse_rrule",
    "repair",
    "simulate",
    "verify",
]