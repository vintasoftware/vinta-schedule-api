"""Errors the public-API aggregation engine raises, and the messages it uses.

Every message a caller can see is written down here once so that no phase of the
aggregation work invents a second wording for the same failure. The wordings
come from the *Errors* table of
``ai-plans/2026-09-11-GRAPHQL_AGGREGATIONS_IMPLEMENTATION_PLAN.md``.

Two rules hold for every message in this module:

* A message never echoes free text the caller supplied. ``Unknown timezone``
  says nothing about the string that was sent, because an IANA name arrives as
  an arbitrary ``String`` and echoing it turns an error into a reflector.
* A message may name a value the GraphQL schema has already constrained to a
  closed set — a group-by field, an aggregate operation. Those reach the engine
  as enum members drawn from this repo's own registry, never as caller free
  text, and naming them is what makes the error diagnosable.

No message ever carries an aggregated value, a group key value or any other row
content: an aggregate over ``CalendarEvent`` reads PHI-bearing columns, and an
error string is not an audited disclosure channel.
"""

from graphql import GraphQLError


LIMIT_OUT_OF_RANGE_MESSAGE = "Limit must be between 1 and 100"
OFFSET_NEGATIVE_MESSAGE = "Offset must be non-negative"
UNKNOWN_TIMEZONE_MESSAGE = "Unknown timezone"
QUERY_TIMEOUT_MESSAGE = "Aggregate query exceeded its time budget"
DUPLICATE_ALIAS_MESSAGE = "Aggregate aliases must be unique across dimensions and metrics"
EMPTY_ALIAS_MESSAGE = "Aggregate aliases must not be empty"
NO_DIMENSIONS_MESSAGE = "An aggregate query must group by at least one dimension"
ENTITY_MISMATCH_MESSAGE = "Aggregate plan and queryset describe different entities"


def date_range_exceeded_message(max_days: int) -> str:
    """Message for a filter whose span is wider than the configured maximum.

    The day count is this repo's own constant, not caller input.
    """
    return f"Date range exceeds the maximum of {max_days} days"


def unknown_aggregate_field_message(field_name: str) -> str:
    """Message for a metric naming a field the entity does not aggregate."""
    return f"Unknown aggregatable field: {field_name}"


def unknown_group_by_field_message(field_name: str) -> str:
    """Message for a dimension naming a field the entity cannot group by."""
    return f"Unknown group-by field: {field_name}"


def unsupported_operation_message(field_name: str, operation: str) -> str:
    """Message for an operation the field's kind does not offer, e.g. ``SUM`` on a string."""
    return f"Operation {operation} is not available on field {field_name}"


class AggregationError(GraphQLError):
    """Base class for every error the aggregation engine raises.

    A ``GraphQLError`` subclass so a resolver can let it propagate: Strawberry
    renders it as a normal GraphQL error entry rather than a 500.
    """


class UnknownAggregateEntityError(AggregationError):
    """Raised when a plan names an entity that is not in the registry."""


class UnknownAggregateFieldError(AggregationError):
    """Raised when a metric or dimension names a field the entity does not expose."""


class UnsupportedAggregateOperationError(AggregationError):
    """Raised when an operation is not available for the field's kind."""


class InvalidAggregatePlanError(AggregationError):
    """Raised when a plan is internally inconsistent — colliding aliases, no dimensions."""


class AggregateRangeTooLargeError(AggregationError):
    """Raised when a filter's date range is wider than the configured maximum."""


class AggregateLimitError(AggregationError):
    """Raised when ``limit`` or ``offset`` falls outside the permitted range."""


class UnknownTimezoneError(AggregationError):
    """Raised when the caller-supplied bucketing timezone is not a valid IANA name."""


class AggregateTimeoutError(AggregationError):
    """Raised when the database aborts an aggregate query on its statement timeout."""
