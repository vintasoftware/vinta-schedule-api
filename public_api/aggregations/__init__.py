"""Entity-agnostic aggregation engine for the public GraphQL API.

A GraphQL selection becomes an :class:`~public_api.aggregations.plan.AggregateQueryPlan`,
and the plan becomes one grouped ORM queryset. Six entity families share the
machinery so they cannot drift into six subtly different behaviours; the
registry is the one place that decides what any of them may be grouped by or
aggregated over.

Modules:
    errors: Caller-facing messages and server-side configuration errors.
    types: The four aggregate output types and ``TemporalGranularity``.
    plan: The frozen dataclasses describing a resolved request.
    registry: Field-kind mapping and the per-entity field registrations.
    executor: Plan -> ``.values(...).annotate(...)`` queryset.

``registry`` and ``executor`` import Django models, so they are re-exported
here rather than imported eagerly at module import -- import them from their
own modules inside an app that is still loading.
"""

from public_api.aggregations.errors import (
    MAX_LIMIT,
    AggregationConfigurationError,
    AliasCollisionError,
    InvalidPlanError,
    UnknownEntityError,
    UnknownFieldError,
    UnsupportedOperationError,
    UnsupportedPlanFeatureError,
)
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    ComparisonOp,
    DimensionSpec,
    FilterBounds,
    HavingSpec,
    MetricComparison,
    MetricSpec,
    OrderDirection,
    OrderSpec,
    WindowSpec,
    dimension_alias,
    metric_alias,
)
from public_api.aggregations.types import (
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
    TemporalGranularity,
)


__all__ = [
    "MAX_LIMIT",
    "AggregatableEntity",
    "AggregateOp",
    "AggregateQueryPlan",
    "AggregationConfigurationError",
    "AliasCollisionError",
    "BooleanAggregate",
    "ComparisonOp",
    "DateTimeAggregate",
    "DimensionSpec",
    "FilterBounds",
    "HavingSpec",
    "InvalidPlanError",
    "MetricComparison",
    "MetricSpec",
    "NumericAggregate",
    "OrderDirection",
    "OrderSpec",
    "StringAggregate",
    "TemporalGranularity",
    "UnknownEntityError",
    "UnknownFieldError",
    "UnsupportedOperationError",
    "UnsupportedPlanFeatureError",
    "WindowSpec",
    "dimension_alias",
    "metric_alias",
]
