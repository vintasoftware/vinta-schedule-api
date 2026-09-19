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
* ``having`` — the comparison inputs and per-entity ``*HavingInput`` that
  filter groups on what they aggregated to.
* ``ordering`` — the per-entity order inputs, and the enums naming a group key
  or a metric to sort by.
* ``rows`` — the per-entity ``*AggregateRow`` output types.
* ``fields`` — the factory that builds the six root fields, their permission
  classes and their cost guards from the registry.
* ``types`` — the four aggregate output types and ``TemporalGranularity``.
* ``errors`` — every message any phase of this feature shows a caller.

``public_api/queries.py`` hangs the six fields ``fields.build_aggregate_field``
returns on ``Query``, and ``public_api/permissions.py`` maps each to the same
resource as the entity's list field.
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
from public_api.aggregations.fields import (
    FILTER_INPUT_TYPES,
    aggregate_field_name,
    build_aggregate_field,
)
from public_api.aggregations.filters import (
    AppointmentTypeAggregateFilterInput,
    AvailableTimeAggregateFilterInput,
    BlockedTimeAggregateFilterInput,
    CalendarAggregateFilterInput,
    CalendarEventAggregateFilterInput,
    CalendarPoolAggregateFilterInput,
)
from public_api.aggregations.having import (
    HAVING_INPUT_TYPES,
    AppointmentTypeHavingInput,
    AvailableTimeHavingInput,
    BlockedTimeHavingInput,
    CalendarEventHavingInput,
    CalendarHavingInput,
    CalendarPoolHavingInput,
    FloatComparison,
    IntComparison,
    NumericAggregateComparison,
    resolve_having,
)
from public_api.aggregations.ordering import (
    METRIC_REF_ENUMS,
    ORDER_INPUT_TYPES,
    ORDERABLE_KEY_ENUMS,
    AppointmentTypeAggregateOrderInput,
    AvailableTimeAggregateOrderInput,
    BlockedTimeAggregateOrderInput,
    CalendarAggregateOrderInput,
    CalendarEventAggregateOrderInput,
    CalendarPoolAggregateOrderInput,
    OrderDirection,
    resolve_order_by,
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
    COUNT_ALIAS,
    REGISTRY,
    AggregatableField,
    EntityRegistration,
    FieldKind,
    GroupableField,
    MetricReference,
    RelationCountField,
    aggregate_type_for,
    build_dimension,
    build_metric,
    concat_alias,
    get_registration,
    metric_alias,
    referenceable_metrics,
    relation_count_alias,
)
from public_api.aggregations.rows import (
    AGGREGATE_ROW_TYPES,
    AppointmentTypeAggregateRow,
    AvailableTimeAggregateRow,
    BlockedTimeAggregateRow,
    CalendarAggregateRow,
    CalendarEventAggregateRow,
    CalendarPoolAggregateRow,
    relation_count_field_name,
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
    "AGGREGATE_ROW_TYPES",
    "COUNT_ALIAS",
    "FILTER_INPUT_TYPES",
    "GROUP_BY_INPUT_TYPES",
    "GROUP_KEY_TYPES",
    "HAVING_INPUT_TYPES",
    "METRIC_REF_ENUMS",
    "ORDERABLE_KEY_ENUMS",
    "ORDER_INPUT_TYPES",
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
    "AppointmentTypeAggregateOrderInput",
    "AppointmentTypeAggregateRow",
    "AppointmentTypeGroupByInput",
    "AppointmentTypeGroupKey",
    "AppointmentTypeHavingInput",
    "AvailableTimeAggregateFilterInput",
    "AvailableTimeAggregateOrderInput",
    "AvailableTimeAggregateRow",
    "AvailableTimeGroupByInput",
    "AvailableTimeGroupKey",
    "AvailableTimeHavingInput",
    "BlockedTimeAggregateFilterInput",
    "BlockedTimeAggregateOrderInput",
    "BlockedTimeAggregateRow",
    "BlockedTimeGroupByInput",
    "BlockedTimeGroupKey",
    "BlockedTimeHavingInput",
    "BooleanAggregate",
    "CalendarAggregateFilterInput",
    "CalendarAggregateOrderInput",
    "CalendarAggregateRow",
    "CalendarEventAggregateFilterInput",
    "CalendarEventAggregateOrderInput",
    "CalendarEventAggregateRow",
    "CalendarEventGroupByInput",
    "CalendarEventGroupKey",
    "CalendarEventHavingInput",
    "CalendarGroupByInput",
    "CalendarGroupKey",
    "CalendarHavingInput",
    "CalendarPoolAggregateFilterInput",
    "CalendarPoolAggregateOrderInput",
    "CalendarPoolAggregateRow",
    "CalendarPoolGroupByInput",
    "CalendarPoolGroupKey",
    "CalendarPoolHavingInput",
    "ComparisonOperator",
    "DateTimeAggregate",
    "DimensionSpec",
    "EntityRegistration",
    "FieldKind",
    "FilterBounds",
    "FloatComparison",
    "GroupableField",
    "HavingCondition",
    "HavingSpec",
    "IntComparison",
    "InvalidAggregatePlanError",
    "MetricReference",
    "MetricSpec",
    "NumericAggregate",
    "NumericAggregateComparison",
    "OrderDirection",
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
    "aggregate_field_name",
    "aggregate_type_for",
    "build_aggregate_field",
    "build_aggregate_queryset",
    "build_dimension",
    "build_group_key",
    "build_metric",
    "concat_alias",
    "execute_aggregate_plan",
    "get_registration",
    "metric_alias",
    "referenceable_metrics",
    "relation_count_alias",
    "relation_count_field_name",
    "resolve_group_by",
    "resolve_group_by_inputs",
    "resolve_having",
    "resolve_order_by",
    "resolve_timezone",
]
