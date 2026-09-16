"""Canonical errors and error messages for the public API aggregation engine.

Two families live here, and the split matters:

- ``AggregationConfigurationError`` and its subclasses signal a *server-side*
  mistake -- a plan that names a field no entity registers, an alias that
  collides with a model column, an operation the field's kind does not
  support. A caller cannot provoke one of these through a valid GraphQL
  document, because the schema built in later phases only offers the
  registered combinations. When one escapes to a request it is a bug in this
  repository, not bad input.
- The ``GraphQLError`` factories below are *caller-facing*. Their wording is
  fixed here, once, so the six entity surfaces cannot each invent their own
  phrasing for the same refusal. Every message is deliberately value-free:
  none of them echoes the caller's input back, which is what keeps an error
  string from becoming a disclosure channel for a rejected timezone, filter
  bound or field value.
"""

from graphql import GraphQLError


#: Maximum ``limit`` an aggregate field accepts. Mirrors the cap
#: ``public_api.queries._slice_qs`` already applies to every list field.
MAX_LIMIT = 100

# --------------------------------------------------------------------------
# Caller-facing messages (API Design -> Errors)
# --------------------------------------------------------------------------

#: Raised when the mandatory filter range spans more than the configured maximum.
DATE_RANGE_EXCEEDED_TEMPLATE = "Date range exceeds the maximum of {max_days} days"

#: Byte-identical to ``public_api.queries._slice_qs`` -- an aggregate field and a
#: list field refuse the same limit with the same sentence.
LIMIT_OUT_OF_RANGE = "Limit must be between 1 and 100"

#: Byte-identical to ``public_api.queries._slice_qs``.
OFFSET_NEGATIVE = "Offset must be non-negative"

#: Never echoes the rejected name back -- see the module docstring.
UNKNOWN_TIMEZONE = "Unknown timezone"

#: Statement-timeout refusal. Discloses nothing about the plan that timed out.
TIME_BUDGET_EXCEEDED = "Aggregate query exceeded its time budget"

#: An aggregate with no dimension is a grand total, which this engine does not
#: produce -- see ``AggregateQueryPlan`` for why.
NO_DIMENSIONS = "At least one groupBy dimension is required"

#: The filter range must be orientable.
INVALID_DATE_RANGE = "The filter's end must not precede its start"


def date_range_exceeded(max_days: int) -> GraphQLError:
    """Build the refusal for a filter range wider than `max_days`."""
    return GraphQLError(DATE_RANGE_EXCEEDED_TEMPLATE.format(max_days=max_days))


def limit_out_of_range() -> GraphQLError:
    """Build the refusal for a ``limit`` outside 1..``MAX_LIMIT``."""
    return GraphQLError(LIMIT_OUT_OF_RANGE)


def offset_negative() -> GraphQLError:
    """Build the refusal for a negative ``offset``."""
    return GraphQLError(OFFSET_NEGATIVE)


def unknown_timezone() -> GraphQLError:
    """Build the refusal for a bucketing timezone that is not an IANA name."""
    return GraphQLError(UNKNOWN_TIMEZONE)


def time_budget_exceeded() -> GraphQLError:
    """Build the refusal for an aggregate that hit the statement timeout."""
    return GraphQLError(TIME_BUDGET_EXCEEDED)


def no_dimensions() -> GraphQLError:
    """Build the refusal for a ``groupBy`` that named no dimension."""
    return GraphQLError(NO_DIMENSIONS)


def invalid_date_range() -> GraphQLError:
    """Build the refusal for a filter range whose end precedes its start."""
    return GraphQLError(INVALID_DATE_RANGE)


# --------------------------------------------------------------------------
# Server-side configuration errors
# --------------------------------------------------------------------------


class AggregationConfigurationError(Exception):
    """Base class for every way the aggregation engine can be misused.

    Never raised in response to a well-formed GraphQL document: the schema the
    later phases generate offers only the entity/field/operation combinations
    the registry declares, so reaching one of these means a plan was built by
    hand -- in a test, or by a resolver that stopped agreeing with the registry.
    """


class UnknownEntityError(AggregationConfigurationError):
    """The plan names an entity that has no registration."""


class UnknownFieldError(AggregationConfigurationError):
    """The plan names a field, relation or dimension the entity does not register.

    Raised rather than silently dropping the metric: a plan that quietly loses
    a metric returns rows that look right and are missing a column.
    """


class UnsupportedOperationError(AggregationConfigurationError):
    """The plan asks a field for an operation its kind does not expose.

    ``SUM`` over a ``CharField`` is the canonical case -- the schema makes it
    unrepresentable, and this is the backstop for a plan built without it.
    """


class AliasCollisionError(AggregationConfigurationError):
    """Two plan entries share an alias, or an alias shadows a model column.

    A dimension and a metric under the same alias would silently overwrite each
    other in the row dict; an alias equal to a concrete column name makes
    Django refuse the annotation outright.
    """


class InvalidPlanError(AggregationConfigurationError):
    """The plan is internally inconsistent -- e.g. an ``orderBy`` naming an
    alias that no dimension or metric produces."""


class UnsupportedPlanFeatureError(AggregationConfigurationError):
    """The plan uses a feature the executor does not implement yet.

    ``having`` lands in Phase 4 and ``window`` in Phase 6. Until then the
    executor refuses a plan carrying either, rather than executing it without
    the clause and returning unfiltered rows that read as filtered ones.
    """


#: Message for the ``having`` half of ``UnsupportedPlanFeatureError``.
HAVING_NOT_IMPLEMENTED = "The aggregate executor does not implement HAVING yet"

#: Message for the ``window`` half of ``UnsupportedPlanFeatureError``.
WINDOW_NOT_IMPLEMENTED = "The aggregate executor does not implement window functions yet"
