"""Errors and error wording for the aggregation engine.

Two families live here, and the split is the point of the module.

``AggregationRequestError`` and its subclasses are what a *caller* can
provoke: an over-long date range, a limit outside the allowed band, a
timezone name Postgres does not know, a query that ran out of time. They are
``GraphQLError``s, so a resolver can let them travel, and their wording comes
from "API Design -> Errors" in
``ai-plans/2026-09-11-GRAPHQL_AGGREGATIONS_IMPLEMENTATION_PLAN.md``. Spelling
them once here is what keeps the six entity surfaces from each inventing
their own phrasing for the same refusal.

``AggregationError`` and its subclasses are the engine talking to the code
that drives it: an unknown field path, an alias that collides, an operation
a field's type does not support. They are plain exceptions and are not meant
to reach a partner -- a resolver that lets one through is a bug in the
resolver, not a message a client should be parsing.

No message in either family echoes caller input back. ``Unknown timezone``
deliberately does not name the timezone it rejected.
"""

from graphql import GraphQLError


# ---------------------------------------------------------------------------
# Caller-visible messages
# ---------------------------------------------------------------------------

#: Raised when the IANA name a caller supplied is not one ``zoneinfo`` knows.
#: The rejected value is deliberately absent -- see the module docstring.
UNKNOWN_TIMEZONE_MESSAGE = "Unknown timezone"

#: Raised when the per-query statement timeout fires. Says nothing about the
#: plan that was running, so a caller cannot probe cost by timing queries.
STATEMENT_TIMEOUT_MESSAGE = "Aggregate query exceeded its time budget"

#: Raised when one entry of ``groupBy`` sets neither its scalar field nor its
#: temporal field, or sets both. GraphQL has no input unions, so the wrapper
#: input carries one nullable slot per variant and this is the residual check
#: the schema cannot make. Names neither slot's value.
GROUP_BY_VARIANT_MESSAGE = "Each groupBy entry must set exactly one of 'field' or 'temporal'"

#: Raised when ``offset`` is negative. Matches ``public_api.queries._slice_qs``'s
#: wording exactly -- aggregate fields reuse this text rather than inventing a
#: second one for the same refusal (see ``LimitOutOfRangeError``).
OFFSET_NEGATIVE_MESSAGE = "Offset must be non-negative"

#: Raised when an ``orderBy`` entry sets neither its scalar ``key`` slot nor
#: its ``metric`` slot, or sets both -- the same "exactly one variant" shape
#: ``groupBy`` uses, for the same reason: GraphQL has no input unions.
ORDER_VARIANT_MESSAGE = "Each orderBy entry must set exactly one of 'key' or 'metric'"

#: Raised when an ``orderBy`` entry's ``key`` names a dimension this query did
#: not actually group by. Names neither the dimension asked for nor the ones
#: available, per the module docstring.
ORDER_KEY_NOT_GROUPED_MESSAGE = "orderBy.key must be one of this query's groupBy dimensions"

#: Raised when an ``IntComparison`` / ``FloatComparison`` sets none of its
#: operators -- a comparison with nothing to compare would match every group,
#: which is never what a caller who wrote a HAVING clause meant.
EMPTY_HAVING_COMPARISON_MESSAGE = "A having comparison must set at least one operator"

#: Raised when a ``*HavingInput`` node sets none of its metric fields and
#: neither of 'and' / 'or' -- the same "nothing to compare" refusal, one
#: level up the tree.
EMPTY_HAVING_INPUT_MESSAGE = "A having input must set at least one field, or 'and' / 'or'"


def date_range_exceeded_message(max_days: int) -> str:
    """Wording for a filter whose datetime range is wider than the maximum."""
    return f"Date range exceeds the maximum of {max_days} days"


def limit_out_of_range_message(minimum: int, maximum: int) -> str:
    """Wording for a ``limit`` argument outside the allowed band.

    Matches ``public_api.queries._slice_qs``'s wording exactly for the same
    ``minimum``/``maximum`` -- see ``LimitOutOfRangeError``.
    """
    return f"Limit must be between {minimum} and {maximum}"


# ---------------------------------------------------------------------------
# Caller-visible errors
# ---------------------------------------------------------------------------


class AggregationRequestError(GraphQLError):
    """Base class for refusals a caller can fix by sending a different query."""


class DateRangeExceededError(AggregationRequestError):
    """The filter's datetime range is wider than the configured maximum."""

    def __init__(self, max_days: int) -> None:
        super().__init__(date_range_exceeded_message(max_days))
        self.max_days = max_days


class LimitOutOfRangeError(AggregationRequestError):
    """``limit`` fell outside the allowed band."""

    def __init__(self, minimum: int, maximum: int) -> None:
        super().__init__(limit_out_of_range_message(minimum, maximum))
        self.minimum = minimum
        self.maximum = maximum


class OffsetOutOfRangeError(AggregationRequestError):
    """``offset`` was negative."""

    def __init__(self) -> None:
        super().__init__(OFFSET_NEGATIVE_MESSAGE)


class UnknownTimezoneError(AggregationRequestError):
    """The supplied bucketing timezone is not a valid IANA name."""

    def __init__(self) -> None:
        super().__init__(UNKNOWN_TIMEZONE_MESSAGE)


class GroupByVariantError(AggregationRequestError):
    """A ``groupBy`` entry set both of its variant slots, or neither."""

    def __init__(self) -> None:
        super().__init__(GROUP_BY_VARIANT_MESSAGE)


class AggregateTimeoutError(AggregationRequestError):
    """The aggregate query hit its per-query statement timeout."""

    def __init__(self) -> None:
        super().__init__(STATEMENT_TIMEOUT_MESSAGE)


class OrderVariantError(AggregationRequestError):
    """An ``orderBy`` entry set both of its variant slots, or neither."""

    def __init__(self) -> None:
        super().__init__(ORDER_VARIANT_MESSAGE)


class OrderKeyNotGroupedError(AggregationRequestError):
    """An ``orderBy`` entry's ``key`` names a dimension this query did not group by."""

    def __init__(self) -> None:
        super().__init__(ORDER_KEY_NOT_GROUPED_MESSAGE)


class EmptyHavingComparisonError(AggregationRequestError):
    """An ``IntComparison`` / ``FloatComparison`` set no operator."""

    def __init__(self) -> None:
        super().__init__(EMPTY_HAVING_COMPARISON_MESSAGE)


class EmptyHavingInputError(AggregationRequestError):
    """A ``*HavingInput`` node set no field and neither of 'and' / 'or'."""

    def __init__(self) -> None:
        super().__init__(EMPTY_HAVING_INPUT_MESSAGE)


# ---------------------------------------------------------------------------
# Engine errors
# ---------------------------------------------------------------------------


class AggregationError(Exception):
    """Base class for engine-level errors, none of which a caller should see."""


class UnknownEntityError(AggregationError):
    """No registry entry exists for the requested entity."""

    def __init__(self, entity: object) -> None:
        super().__init__(f"No aggregate registration for entity {entity!r}")
        self.entity = entity


class UnknownDimensionError(AggregationError):
    """A plan named a dimension the entity does not expose as groupable."""

    def __init__(self, entity: object, field_path: str) -> None:
        super().__init__(f"{entity!r} has no groupable dimension {field_path!r}")
        self.entity = entity
        self.field_path = field_path


class UnknownMetricFieldError(AggregationError):
    """A plan named a field the entity does not expose as aggregatable."""

    def __init__(self, entity: object, field_path: str) -> None:
        super().__init__(f"{entity!r} has no aggregatable field {field_path!r}")
        self.entity = entity
        self.field_path = field_path


class UnsupportedOperationError(AggregationError):
    """The requested operation is not one this field's type supports.

    ``concat`` over a numeric column is the canonical case: the schema
    prevents it for GraphQL callers, and this is the same refusal for code
    that builds a plan directly.
    """

    def __init__(self, op: object, field_path: str, kind: object) -> None:
        super().__init__(f"Operation {op!r} is not supported for {kind!r} field {field_path!r}")
        self.op = op
        self.field_path = field_path
        self.kind = kind


class AliasCollisionError(AggregationError):
    """Two parts of a plan asked for the same alias, or an alias shadows a column.

    A row dict has one slot per alias, so a collision silently drops one of
    the two values rather than failing -- which is why this is checked rather
    than discovered.
    """


class InvalidPlanError(AggregationError):
    """The plan is internally inconsistent and cannot be executed."""


class NonTemporalGranularityError(AggregationError):
    """A granularity was set on a dimension the registry does not call temporal.

    A GraphQL caller cannot reach this: the scalar and temporal group-by
    enums are disjoint, so asking to bucket a foreign key fails schema
    validation. It catches a plan built in code, where nothing else would --
    ``DATE_TRUNC`` over a non-timestamp column is a database error at best
    and a meaningless bucket at worst.
    """


class UnsupportedPlanFeatureError(AggregationError):
    """The plan asks for a feature this phase of the engine does not build yet.

    A loud refusal rather than a silent drop: a window clause that is quietly
    ignored returns plausible numbers that are not the ones asked for.
    """


class QuerysetModelMismatchError(AggregationError):
    """The base queryset is over a different model than the plan's entity."""

    def __init__(self, expected: object, received: object) -> None:
        super().__init__(f"Expected a queryset over {expected!r}, received one over {received!r}")
        self.expected = expected
        self.received = received
