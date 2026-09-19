"""Turning a caller-supplied IANA name into a ``ZoneInfo``.

Bucketing needs a timezone because this project stores a local wall clock plus
the row's own IANA name, so "per day" is genuinely ambiguous until somebody
names the clock. The caller names it, once per query, and every bucket in the
result is cut on that one clock — rather than each row being bucketed on its own
local day, which would silently mix wall clocks inside a single result set.
"""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from public_api.aggregations.errors import UNKNOWN_TIMEZONE_MESSAGE, UnknownTimezoneError


def resolve_timezone(name: str) -> ZoneInfo:
    """Return the ``ZoneInfo`` for an IANA name, or raise ``UnknownTimezoneError``.

    The timezone is the one aggregate argument that arrives as free text, so the
    error says only ``Unknown timezone``: it never repeats what was sent, and
    the cause is suppressed so nothing downstream can format the original
    exception's message — ``ZoneInfoNotFoundError`` carries the rejected string.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise UnknownTimezoneError(UNKNOWN_TIMEZONE_MESSAGE) from None
