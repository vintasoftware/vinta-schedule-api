"""Entity-agnostic aggregation engine for the public GraphQL API.

Six entity families are grouped and aggregated through one engine rather than
six, so a behaviour decided once -- which operations a string field offers, how
a relation is counted without join fan-out, what an over-long limit is called --
holds everywhere instead of six times over.

The pieces, in the order a request moves through them:

* :mod:`~public_api.aggregations.types` -- the four aggregate output types and
  the bucketing enum. Which type a field gets is what stops ``title { sum }``
  at GraphQL validation, before a resolver runs.
* :mod:`~public_api.aggregations.registry` -- the one table saying which fields
  of which entity are groupable and aggregatable, and as what.
* :mod:`~public_api.aggregations.plan` -- the frozen description of one
  request, built from a GraphQL selection before any ORM call.
* :mod:`~public_api.aggregations.executor` -- the plan turned into a single
  ``.values(...).annotate(...)`` queryset over a caller-supplied, already
  organization-scoped base.
* :mod:`~public_api.aggregations.errors` -- every message these raise.

Nothing here is attached to the GraphQL schema yet; the root fields that expose
it arrive in a later phase of the plan.
"""

from public_api.aggregations.errors import (
    AggregateConfigurationError,
    AggregateError,
    AggregateQueryTimeoutError,
    AggregateRegistrationError,
    AliasCollisionError,
    ConcatArgumentsMismatchError,
    DateRangeTooLargeError,
    EmptyAggregatePlanError,
    EntityQuerysetMismatchError,
    LimitOutOfRangeError,
    MissingBucketTimezoneError,
    OffsetNegativeError,
    ReservedAliasError,
    UnknownAggregateEntityError,
    UnknownAggregateFieldError,
    UnknownDimensionError,
    UnknownOrderAliasError,
    UnknownTimezoneError,
    UnsupportedAggregateOperationError,
    WindowNotSupportedError,
)
from public_api.aggregations.executor import build_aggregate_queryset, execute_plan
from public_api.aggregations.plan import (
    MAX_LIMIT,
    MIN_LIMIT,
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    DimensionSpec,
    FilterBounds,
    HavingSpec,
    MetricSpec,
    OrderSpec,
    WindowSpec,
)
from public_api.aggregations.registry import (
    DEFAULT_METRIC_OPTIONS,
    REGISTRY,
    AggregatableField,
    AggregateKind,
    EntityRegistration,
    GroupableField,
    RelationCount,
    aggregate_kind_for_model_field,
    concrete_output_field,
    dimension_alias,
    get_registration,
    metric_alias,
    validate_plan,
)
from public_api.aggregations.types import (
    DEFAULT_CONCAT_SEPARATOR,
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
    TemporalGranularity,
)


__all__ = [
    "DEFAULT_CONCAT_SEPARATOR",
    "DEFAULT_METRIC_OPTIONS",
    "MAX_LIMIT",
    "MIN_LIMIT",
    "REGISTRY",
    "AggregatableEntity",
    "AggregatableField",
    "AggregateConfigurationError",
    "AggregateError",
    "AggregateKind",
    "AggregateOp",
    "AggregateQueryPlan",
    "AggregateQueryTimeoutError",
    "AggregateRegistrationError",
    "AliasCollisionError",
    "BooleanAggregate",
    "ConcatArgumentsMismatchError",
    "DateRangeTooLargeError",
    "DateTimeAggregate",
    "DimensionSpec",
    "EmptyAggregatePlanError",
    "EntityQuerysetMismatchError",
    "EntityRegistration",
    "FilterBounds",
    "GroupableField",
    "HavingSpec",
    "LimitOutOfRangeError",
    "MetricSpec",
    "MissingBucketTimezoneError",
    "NumericAggregate",
    "OffsetNegativeError",
    "OrderSpec",
    "RelationCount",
    "ReservedAliasError",
    "StringAggregate",
    "TemporalGranularity",
    "UnknownAggregateEntityError",
    "UnknownAggregateFieldError",
    "UnknownDimensionError",
    "UnknownOrderAliasError",
    "UnknownTimezoneError",
    "UnsupportedAggregateOperationError",
    "WindowNotSupportedError",
    "WindowSpec",
    "aggregate_kind_for_model_field",
    "build_aggregate_queryset",
    "concrete_output_field",
    "dimension_alias",
    "execute_plan",
    "get_registration",
    "metric_alias",
    "validate_plan",
]
