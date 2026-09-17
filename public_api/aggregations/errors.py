"""Error wording for the aggregation engine, in one place.

Every phase of the GraphQL aggregations plan raises from here rather than
writing its own message, so a partner sees the same sentence for the same
condition no matter which entity or which layer refused the query. The
messages are the ones the plan's *API Design -> Errors* table publishes.

Two families live here:

* :class:`AggregateError` and its subclasses are **partner-facing**. They are
  ``GraphQLError``s, so Strawberry returns them in the ``errors`` array with
  the message intact. None of them echoes caller input back -- an unknown
  timezone name is refused without repeating the string, because that string
  reaches the response body and the logs.
* :class:`AggregateConfigurationError` and its subclasses are **internal**.
  They mean a plan was built that the registry does not describe -- an unknown
  field path, an operation a field's type does not support, two selections
  claiming the same alias. Every one of those is reachable only from code in
  this repository (the GraphQL schema restricts a caller to registered
  enum values), so they deliberately do not carry a partner-facing message.
"""

from graphql import GraphQLError


# Partner-facing messages, verbatim from the plan's API Design -> Errors table.
DATE_RANGE_TOO_LARGE_TEMPLATE = "Date range exceeds the maximum of {max_days} days"
LIMIT_OUT_OF_RANGE_MESSAGE = "Limit must be between 1 and 100"
OFFSET_NEGATIVE_MESSAGE = "Offset must be non-negative"
UNKNOWN_TIMEZONE_MESSAGE = "Unknown timezone"
QUERY_TIMEOUT_MESSAGE = "Aggregate query exceeded its time budget"


class AggregateError(GraphQLError):
    """Base class for every aggregate failure a partner is allowed to see."""


class DateRangeTooLargeError(AggregateError):
    """The requested range is longer than the configured maximum span."""

    def __init__(self, max_days: int) -> None:
        super().__init__(DATE_RANGE_TOO_LARGE_TEMPLATE.format(max_days=max_days))


class LimitOutOfRangeError(AggregateError):
    """``limit`` fell outside 1-100, the same bound every list field applies."""

    def __init__(self) -> None:
        super().__init__(LIMIT_OUT_OF_RANGE_MESSAGE)


class OffsetNegativeError(AggregateError):
    """``offset`` was negative."""

    def __init__(self) -> None:
        super().__init__(OFFSET_NEGATIVE_MESSAGE)


class UnknownTimezoneError(AggregateError):
    """The bucketing timezone is not a name the runtime knows.

    The offending value is deliberately not repeated in the message: it is
    caller-supplied text that would otherwise land in the response body and in
    every log line that records the error.
    """

    def __init__(self) -> None:
        super().__init__(UNKNOWN_TIMEZONE_MESSAGE)


class AggregateQueryTimeoutError(AggregateError):
    """The statement timeout fired. No detail about the plan is disclosed."""

    def __init__(self) -> None:
        super().__init__(QUERY_TIMEOUT_MESSAGE)


class AggregateConfigurationError(Exception):
    """A plan that the registry cannot describe. Always a bug in this repo."""


class AggregateRegistrationError(AggregateConfigurationError):
    """A registry entry disagrees with the model it describes.

    Raised at import time, deliberately: a field renamed on a model should stop
    the process rather than surface as one partner's failing query weeks later.
    """


class UnknownAggregateEntityError(AggregateConfigurationError):
    """No entity is registered under that name."""

    def __init__(self, entity: object) -> None:
        super().__init__(f"No aggregate registration for entity {entity!r}")


class UnknownAggregateFieldError(AggregateConfigurationError):
    """A metric named a field the entity does not expose as aggregatable.

    Raised rather than dropping the metric: a plan that silently loses a
    requested aggregate returns a row whose missing key looks like a null
    result instead of like the mistake it is.
    """

    def __init__(self, entity: object, field_path: str) -> None:
        super().__init__(f"{field_path!r} is not an aggregatable field of {entity!r}")


class UnknownDimensionError(AggregateConfigurationError):
    """A dimension named a field the entity does not expose as groupable."""

    def __init__(self, entity: object, field_path: str) -> None:
        super().__init__(f"{field_path!r} is not a groupable field of {entity!r}")


class UnsupportedAggregateOperationError(AggregateConfigurationError):
    """The operation is not one the field's type offers.

    This is the rule that keeps ``sum`` off a ``CharField``: the registry maps
    each field to exactly one aggregate kind, and each kind to a closed set of
    operations.
    """

    def __init__(self, entity: object, field_path: str, op: object) -> None:
        super().__init__(f"{op!r} is not available on {field_path!r} of {entity!r}")


class AliasCollisionError(AggregateConfigurationError):
    """Two selections in one plan claim the same row-dictionary key.

    Row keys are flat, so a dimension and a metric sharing an alias would make
    one of them unreadable -- and, worse, would make it unreadable *silently*,
    with the second annotation overwriting the first.
    """

    def __init__(self, alias: str) -> None:
        super().__init__(f"Alias {alias!r} is used more than once in one aggregate plan")


class MissingBucketTimezoneError(AggregateConfigurationError):
    """A temporal dimension asks for a bucket size but names no timezone.

    Left unset, ``Trunc*`` falls back to Django's *current* timezone, so the
    bucket boundary becomes the server's midnight rather than the one the
    caller named. That is the alternative the plan explicitly rejected: it
    mixes wall clocks inside one result set and returns a plausible wrong
    answer instead of an error. Refused here so it cannot happen at all.
    """

    def __init__(self, field_path: str) -> None:
        super().__init__(
            f"Bucketing {field_path!r} needs an explicit timezone: a granularity "
            f"without one buckets on the server's clock, not the caller's"
        )


class ConcatArgumentsMismatchError(AggregateConfigurationError):
    """``concat`` was selected with arguments the executed query did not use.

    ``separator`` and ``distinct`` change the SQL, so they have to be read off
    the selection when the plan is built. If the resolver that built the plan
    ignored them, the string returned here answers a different question from
    the one the caller asked -- and would do so silently. Raised instead.
    """

    def __init__(self) -> None:
        super().__init__(
            "concat() was resolved with arguments the aggregate query was not built "
            "for; the plan builder must read separator/distinct off the selection"
        )


class UnknownOrderAliasError(AggregateConfigurationError):
    """A plan orders by an alias none of its dimensions or metrics produces.

    Not a harmless no-op: Django would resolve the unknown name against the
    model instead, which adds a column to the GROUP BY and changes the grouping
    the caller asked for.
    """

    def __init__(self, alias: str) -> None:
        super().__init__(f"Cannot order by {alias!r}: no dimension or metric produces it")


class ReservedAliasError(AggregateConfigurationError):
    """An alias collides with a concrete column of the model being grouped.

    Django refuses an annotation whose name matches a model field, so this is
    caught here with a message that says which alias to rename.
    """

    def __init__(self, alias: str, model: object) -> None:
        super().__init__(f"Alias {alias!r} collides with a field of {model!r}; rename it")


class EmptyAggregatePlanError(AggregateConfigurationError):
    """A plan with no dimensions, or with no metrics.

    Neither is a harmless no-op. ``.values()`` with no arguments groups by
    *every* column, which turns an aggregate into a full row dump; and a plan
    with no metrics computes nothing.
    """


class WindowNotSupportedError(AggregateConfigurationError):
    """A plan carries a window, which this engine does not execute yet.

    Window construction lands in its own phase (see the plan's Phase 6). Until
    it does, a plan that sets ``window`` is refused rather than executed with
    the window quietly dropped.
    """

    def __init__(self) -> None:
        super().__init__("Window functions are not implemented by the aggregate executor yet")


class EntityQuerysetMismatchError(AggregateConfigurationError):
    """The base queryset is over a different model than the plan's entity."""

    def __init__(self, entity: object, expected: object, received: object) -> None:
        super().__init__(
            f"Plan for {entity!r} expects a queryset over {expected!r}, got {received!r}"
        )
