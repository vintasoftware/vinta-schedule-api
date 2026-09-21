"""Entity-agnostic aggregation engine for the public GraphQL API.

A caller builds an :class:`~public_api.aggregations.plan.AggregateQueryPlan`
from a GraphQL selection, hands it plus an already-scoped base queryset to
:func:`~public_api.aggregations.executor.build_aggregate_queryset`, and gets
back one row dict per non-empty group -- grouped and aggregated by Postgres,
in one query, with nothing reshaped in Python.

Which fields an entity may be grouped by or aggregated over, and which
operations each field's type accepts, is decided in
:mod:`public_api.aggregations.registry` and nowhere else.

The base queryset itself comes from a per-entity filter input in
:mod:`public_api.aggregations.filters`, which starts from the model's scoped
manager and narrows it to the caller's bounded date range.

Nothing here is on the schema yet. Later phases of
``ai-plans/2026-09-11-GRAPHQL_AGGREGATIONS_IMPLEMENTATION_PLAN.md`` add
temporal bucketing, the six root fields, HAVING, audit logging, window
functions and nested batching on top.
"""

from public_api.aggregations.errors import (
    AggregateTimeoutError,
    AggregationError,
    AggregationRequestError,
    AliasCollisionError,
    DateRangeExceededError,
    InvalidPlanError,
    LimitOutOfRangeError,
    QuerysetModelMismatchError,
    UnknownDimensionError,
    UnknownEntityError,
    UnknownMetricFieldError,
    UnknownTimezoneError,
    UnsupportedOperationError,
    UnsupportedPlanFeatureError,
)
from public_api.aggregations.executor import build_aggregate_queryset
from public_api.aggregations.filters import (
    AppointmentTypeAggregateFilterInput,
    AvailableTimeAggregateFilterInput,
    BlockedTimeAggregateFilterInput,
    CalendarAggregateFilterInput,
    CalendarEventAggregateFilterInput,
    CalendarPoolAggregateFilterInput,
)
from public_api.aggregations.plan import (
    MAX_AGGREGATE_LIMIT,
    MIN_AGGREGATE_LIMIT,
    ROW_COUNT_FIELD_PATH,
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    DimensionSpec,
    FilterBounds,
    HavingSpec,
    MetricSpec,
    OrderDirection,
    OrderSpec,
    WindowSpec,
)
from public_api.aggregations.registry import (
    AGGREGATE_TYPE_BY_KIND,
    OPS_BY_KIND,
    AggregatableField,
    EntityRegistration,
    FieldKind,
    GroupableDimension,
    RelationCount,
    aggregate_type_for,
    get_registration,
    registered_entities,
    supported_ops,
)
from public_api.aggregations.types import (
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
    TemporalGranularity,
)


__all__ = [
    "AGGREGATE_TYPE_BY_KIND",
    "MAX_AGGREGATE_LIMIT",
    "MIN_AGGREGATE_LIMIT",
    "OPS_BY_KIND",
    "ROW_COUNT_FIELD_PATH",
    "AggregatableEntity",
    "AggregatableField",
    "AggregateOp",
    "AggregateQueryPlan",
    "AggregateTimeoutError",
    "AggregationError",
    "AggregationRequestError",
    "AliasCollisionError",
    "AppointmentTypeAggregateFilterInput",
    "AvailableTimeAggregateFilterInput",
    "BlockedTimeAggregateFilterInput",
    "BooleanAggregate",
    "CalendarAggregateFilterInput",
    "CalendarEventAggregateFilterInput",
    "CalendarPoolAggregateFilterInput",
    "DateRangeExceededError",
    "DateTimeAggregate",
    "DimensionSpec",
    "EntityRegistration",
    "FieldKind",
    "FilterBounds",
    "GroupableDimension",
    "HavingSpec",
    "InvalidPlanError",
    "LimitOutOfRangeError",
    "MetricSpec",
    "NumericAggregate",
    "OrderDirection",
    "OrderSpec",
    "QuerysetModelMismatchError",
    "RelationCount",
    "StringAggregate",
    "TemporalGranularity",
    "UnknownDimensionError",
    "UnknownEntityError",
    "UnknownMetricFieldError",
    "UnknownTimezoneError",
    "UnsupportedOperationError",
    "UnsupportedPlanFeatureError",
    "WindowSpec",
    "aggregate_type_for",
    "build_aggregate_queryset",
    "get_registration",
    "registered_entities",
    "supported_ops",
]
