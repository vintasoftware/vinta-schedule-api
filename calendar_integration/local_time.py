"""Turn a local wall-clock time into a UTC instant the way Postgres does.

This module imports no models, so models and utilities can both use it without
an import cycle.

Some local times are special on the day the clocks change:

- A time that happens twice (clocks go back): Postgres picks the later one, the
  standard-time instant. Python's ``ZoneInfo`` picks the earlier one by default.
- A time that does not exist (clocks go forward): Postgres uses the offset from
  before the change. Python does the same with the default ``fold=0``.

The recurrence functions in Postgres return occurrence instants, and recurrence
exceptions are matched against them by exact instant, so Python has to get the
same instant for the same local time.
"""

import datetime
import zoneinfo


def local_wall_clock_to_utc(wall_clock: datetime.datetime, tz_name: str) -> datetime.datetime:
    """Return the UTC instant of ``wall_clock`` read in the IANA zone ``tz_name``.

    Any tzinfo on ``wall_clock`` is dropped: only its date and time are used.
    """
    zone = zoneinfo.ZoneInfo(tz_name)
    naive = wall_clock.replace(tzinfo=None)
    earlier = naive.replace(tzinfo=zone, fold=0)
    later = naive.replace(tzinfo=zone, fold=1)

    if earlier.utcoffset() != later.utcoffset():
        earlier_utc = earlier.astimezone(datetime.UTC)
        later_utc = later.astimezone(datetime.UTC)
        # If both instants read back as the same wall-clock, that time happens
        # twice: take the later one. Otherwise it falls in a gap and does not
        # exist: keep the offset from before the change.
        is_ambiguous = (
            earlier_utc.astimezone(zone).replace(tzinfo=None) == naive
            and later_utc.astimezone(zone).replace(tzinfo=None) == naive
        )
        if is_ambiguous:
            return later_utc

    return earlier.astimezone(datetime.UTC)
