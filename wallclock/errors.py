"""Exception hierarchy for wallclock.

Everything a user can get wrong (bad cron syntax, unknown zone, impossible
contract) raises a subclass of :class:`WallclockError`, so the CLI can turn it
into a single-line diagnostic instead of a traceback.
"""

from __future__ import annotations


class WallclockError(Exception):
    """Base class for all wallclock user-facing errors."""


class SpecError(WallclockError, ValueError):
    """A cron expression / RRULE / policy / contract is malformed."""


class ZoneError(WallclockError, ValueError):
    """An unknown or unusable timezone."""


class HorizonError(WallclockError, ValueError):
    """The requested analysis horizon is invalid or too wide."""