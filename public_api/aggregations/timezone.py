"""Turning a caller-supplied IANA timezone name into a :class:`~zoneinfo.ZoneInfo`.

Bucketing needs a wall clock, and the plan's guiding decision is that the caller
names it rather than inheriting the server's. That makes the timezone name
caller-supplied text reaching the database layer, so it is resolved in exactly
one place, and an unresolvable name is refused here rather than three modules
later.

Two properties this module exists to hold:

* **The name is never echoed back.** ``ZoneInfo``'s own exceptions quote the key
  they failed on, so re-raising one would put caller text into the GraphQL
  response body and into every log line recording the error. The chain is
  suppressed and the message is the fixed one the plan publishes.
* **A name is only ever a key, never a path.** ``ZoneInfo`` resolves its
  argument against the zoneinfo search path, so a caller-supplied value is a
  filesystem lookup. ``zoneinfo`` itself rejects absolute paths and ``..``
  segments with ``ValueError``, and the membership check below refuses anything
  outside the runtime's own list before it gets that far.
"""

import zoneinfo

from public_api.aggregations.errors import UnknownTimezoneError


def resolve_timezone(name: str) -> zoneinfo.ZoneInfo:
    """Return the zone ``name`` identifies, or raise :class:`UnknownTimezoneError`.

    Accepts only names the runtime's own tz database lists. That is stricter
    than ``ZoneInfo(name)`` alone -- which would also accept whatever else
    happens to sit on the zoneinfo search path -- and it is what makes the
    accepted set the same one a partner can read from the tz database rather
    than a property of this host's filesystem.
    """
    if not isinstance(name, str) or name not in zoneinfo.available_timezones():
        raise UnknownTimezoneError

    try:
        return zoneinfo.ZoneInfo(name)
    except (ValueError, zoneinfo.ZoneInfoNotFoundError, OSError):
        # ``from None``: the originals quote the offending key, and this error's
        # message is published to the caller.
        raise UnknownTimezoneError from None
