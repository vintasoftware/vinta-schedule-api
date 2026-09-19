"""The public API's aggregation engine.

Entity-agnostic machinery that turns a GraphQL selection into an ORM-executable
aggregate plan and runs it as a single grouped query. Six entity families share
it, which is the point: six wirings of the same idea is how the same idea ends
up behaving six slightly different ways.

The layers, in the order a request moves through them:

* ``registry`` — what each entity can group by and aggregate, and which
  Strawberry type each field kind is exposed as.
* ``plan`` — the frozen ``AggregateQueryPlan`` built from the selection, before
  any ORM call. Audited as-is.
* ``filters`` — the per-entity input types that narrow an entity's scoped
  queryset, with the bounded date range required at the type level.
* ``dimensions`` — the per-entity group-by inputs and ``*GroupKey`` output
  types, split into a scalar and a temporal variant so a granularity on a
  non-temporal field cannot be written down.
* ``timezone`` — IANA name validation for the bucketing clock.
* ``executor`` — the plan as ``.values(...).annotate(...)`` over a
  caller-supplied, already-scoped queryset.
* ``types`` — the four aggregate output types and ``TemporalGranularity``.
* ``errors`` — every message any phase of this feature shows a caller.

Nothing here is reachable from the GraphQL schema yet; Phase 3 of
``ai-plans/2026-09-11-GRAPHQL_AGGREGATIONS_IMPLEMENTATION_PLAN.md`` registers the
root fields.
"""

from public_api.aggregations.dimensions import (
    GROUP_BY_INPUT_TYPES,
    GROUP_KEY_TYPES,
    SCALAR_GROUP_BY_ENUMS,
    TEMPORAL_GROUP_BY_ENUMS,
    AppointmentTypeGroupByInput,
    AppointmentTypeGroupKey,
    AvailableTimeGroupByInput,
    AvailableTimeGroupKey,
    BlockedTimeGroupByInput,
    BlockedTimeGroupKey,
    CalendarEventGroupByInput,
    CalendarEventGroupKey,
    CalendarGroupByInput,
    CalendarGroupKey,
    CalendarPoolGroupByInput,
    CalendarPoolGroupKey,
    ResolvedGroupBy,
    build_group_key,
    resolve_group_by,
    resolve_group_by_inputs,
)
from public_api.aggregations.errors import (
    AggregateLimitError,
    AggregateRangeTooLargeError,
    AggregateTimeoutError,
    AggregationError,
    InvalidAggregatePlanError,
    UnknownAggregateEntityError,
    UnknownAggregateFieldError,
    UnknownTimezoneError,
    UnsupportedAggregateOperationError,
)
from public_api.aggregations.executor import (
    build_aggregate_queryset,
    execute_aggregate_plan,
)
from public_api.aggregations.filters import (
    AppointmentTypeAggregateFilterInput,
    AvailableTimeAggregateFilterInput,
    BlockedTimeAggregateFilterInput,
    CalendarAggregateFilterInput,
    CalendarEventAggregateFilterInput,
    CalendarPoolAggregateFilterInput,
)
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    ComparisonOperator,
    DimensionSpec,
    FilterBounds,
    HavingCondition,
    HavingSpec,
    MetricSpec,
    OrderSpec,
    WindowBound,
    WindowFrameSpec,
    WindowFrameType,
    WindowSpec,
)
from public_api.aggregations.registry import (
    REGISTRY,
    AggregatableField,
    EntityRegistration,
    FieldKind,
    GroupableField,
    RelationCountField,
    aggregate_type_for,
    build_dimension,
    build_metric,
    get_registration,
)
from public_api.aggregations.timezone import resolve_timezone
from public_api.aggregations.types import (
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
    TemporalGranularity,
)


__all__ = [
    "GROUP_BY_INPUT_TYPES",
    "GROUP_KEY_TYPES",
    "REGISTRY",
    "SCALAR_GROUP_BY_ENUMS",
    "TEMPORAL_GROUP_BY_ENUMS",
    "AggregatableEntity",
    "AggregatableField",
    "AggregateLimitError",
    "AggregateOp",
    "AggregateQueryPlan",
    "AggregateRangeTooLargeError",
    "AggregateTimeoutError",
    "AggregationError",
    "AppointmentTypeAggregateFilterInput",
    "AppointmentTypeGroupByInput",
    "AppointmentTypeGroupKey",
    "AvailableTimeAggregateFilterInput",
    "AvailableTimeGroupByInput",
    "AvailableTimeGroupKey",
    "BlockedTimeAggregateFilterInput",
    "BlockedTimeGroupByInput",
    "BlockedTimeGroupKey",
    "BooleanAggregate",
    "CalendarAggregateFilterInput",
    "CalendarEventAggregateFilterInput",
    "CalendarEventGroupByInput",
    "CalendarEventGroupKey",
    "CalendarGroupByInput",
    "CalendarGroupKey",
    "CalendarPoolAggregateFilterInput",
    "CalendarPoolGroupByInput",
    "CalendarPoolGroupKey",
    "ComparisonOperator",
    "DateTimeAggregate",
    "DimensionSpec",
    "EntityRegistration",
    "FieldKind",
    "FilterBounds",
    "GroupableField",
    "HavingCondition",
    "HavingSpec",
    "InvalidAggregatePlanError",
    "MetricSpec",
    "NumericAggregate",
    "OrderSpec",
    "RelationCountField",
    "ResolvedGroupBy",
    "StringAggregate",
    "TemporalGranularity",
    "UnknownAggregateEntityError",
    "UnknownAggregateFieldError",
    "UnknownTimezoneError",
    "UnsupportedAggregateOperationError",
    "WindowBound",
    "WindowFrameSpec",
    "WindowFrameType",
    "WindowSpec",
    "aggregate_type_for",
    "build_aggregate_queryset",
    "build_dimension",
    "build_group_key",
    "build_metric",
    "execute_aggregate_plan",
    "get_registration",
    "resolve_group_by",
    "resolve_group_by_inputs",
    "resolve_timezone",
]
