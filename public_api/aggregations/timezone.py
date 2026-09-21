"""Resolve the caller-supplied IANA name a temporal dimension buckets in.

"Per day" is genuinely ambiguous on these models: a row stores
``start_time_tz_unaware`` (a local wall clock) plus its own ``timezone``
column, and ``start_time`` is generated from the pair. Bucketing on each
row's own local day would silently mix wall clocks inside one result set --
a multi-region organization's "Monday" would mean several different
24-hour windows in the same response. The caller names one timezone instead
and every bucket in the result is measured against it.

The name reaches us from a GraphQL argument, so it is untrusted input and
its only use is to build a :class:`~zoneinfo.ZoneInfo`. A bad one is refused
with the plan's fixed wording, which never repeats the value back.
"""

from zoneinfo import ZoneInfo

from public_api.aggregations.errors import UnknownTimezoneError


def resolve_bucketing_timezone(name: str) -> ZoneInfo:
    """Return the :class:`ZoneInfo` for ``name``, or refuse it.

    ``zoneinfo`` reports an unusable name three different ways -- a missing
    zone raises ``ZoneInfoNotFoundError`` (a ``KeyError``), a name that is
    not a legal key at all (``""``, anything with a ``..`` segment, an
    absolute path) raises ``ValueError``, and an unreadable tzdata entry
    raises ``OSError``. All three mean the same thing to a caller, and all
    three get the same answer.

    The rejected value is deliberately absent from the error: the name is
    caller input, the message is a fixed string in
    :mod:`public_api.aggregations.errors`, and ``from None`` keeps the
    original exception -- which does quote the name -- out of the chain.
    """
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        raise UnknownTimezoneError() from None
