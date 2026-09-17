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
* :mod:`~public_api.aggregations.filters` -- the per-entity filter inputs that
  narrow an entity's scoped queryset, with the bounded date range made
  mandatory at the type level. This is what produces the base queryset the
  executor aggregates over.
* :mod:`~public_api.aggregations.dimensions` -- the per-entity group-by enums
  and inputs, and the ``*GroupKey`` types a grouped row comes back under. The
  split between a scalar and a temporal variant is what makes "a bucket size on
  a non-temporal field" a document GraphQL refuses rather than a runtime check.
* :mod:`~public_api.aggregations.timezone` -- the one place a caller-supplied
  IANA name becomes a ``ZoneInfo``, and the one place an unusable one is
  refused without echoing it back.
* :mod:`~public_api.aggregations.plan` -- the frozen description of one
  request, built from a GraphQL selection before any ORM call.
* :mod:`~public_api.aggregations.having` -- the comparison inputs and per-entity
  ``*HavingInput`` that drop whole groups on the strength of what they
  aggregated to, and the conversion that hands back both the ``Q`` and the
  metrics that ``Q`` needs annotated.
* :mod:`~public_api.aggregations.ordering` -- the per-entity order inputs, and
  the generated enums naming a group-key dimension or a metric to sort on.
  Together with the limit, this is what makes top-N expressible.
* :mod:`~public_api.aggregations.windows` -- the window inputs and the per-entity
  ``*WindowMetrics`` types, for the questions about a row's place in the
  *sequence* of rows that no amount of grouping can answer.
* :mod:`~public_api.aggregations.executor` -- the plan turned into a single
  ``.values(...).annotate(...)`` queryset over a caller-supplied, already
  organization-scoped base.
* :mod:`~public_api.aggregations.fields` -- the six root fields, built from the
  registry by one factory, and the cost guards that bound what one may ask for.
  This is the only module here that touches the published schema.
* :mod:`~public_api.aggregations.errors` -- every message these raise.
"""

from public_api.aggregations.dimensions import (
    GROUP_BY_INPUT_TYPE_BY_ENTITY,
    GROUP_KEY_TYPE_BY_ENTITY,
    AppointmentTypeGroupByInput,
    AppointmentTypeGroupKey,
    AppointmentTypeScalarGroupByField,
    AppointmentTypeTemporalGroupBy,
    AppointmentTypeTemporalGroupByField,
    AvailableTimeGroupByInput,
    AvailableTimeGroupKey,
    AvailableTimeScalarGroupByField,
    AvailableTimeTemporalGroupBy,
    AvailableTimeTemporalGroupByField,
    BlockedTimeGroupByInput,
    BlockedTimeGroupKey,
    BlockedTimeScalarGroupByField,
    BlockedTimeTemporalGroupBy,
    BlockedTimeTemporalGroupByField,
    CalendarEventGroupByInput,
    CalendarEventGroupKey,
    CalendarEventScalarGroupByField,
    CalendarEventTemporalGroupBy,
    CalendarEventTemporalGroupByField,
    CalendarGroupByInput,
    CalendarGroupKey,
    CalendarPoolGroupByInput,
    CalendarPoolGroupKey,
    CalendarPoolTemporalGroupBy,
    CalendarPoolTemporalGroupByField,
    CalendarScalarGroupByField,
    CalendarTemporalGroupBy,
    CalendarTemporalGroupByField,
    build_group_key,
    dimensions_from_group_by,
)
from public_api.aggregations.errors import (
    AggregateConfigurationError,
    AggregateError,
    AggregateQueryTimeoutError,
    AggregateRegistrationError,
    AliasCollisionError,
    AmbiguousGroupByError,
    AmbiguousOrderInputError,
    ConcatArgumentsMismatchError,
    DateRangeTooLargeError,
    DuplicateGroupKeyFieldError,
    EmptyAggregatePlanError,
    EntityQuerysetMismatchError,
    LimitOutOfRangeError,
    MisplacedFrameBoundError,
    MissingBucketTimezoneError,
    OffsetNegativeError,
    ReservedAliasError,
    UngroupedOrderKeyError,
    UngroupedPartitionKeyError,
    UnknownAggregateEntityError,
    UnknownAggregateFieldError,
    UnknownDimensionError,
    UnknownHavingAliasError,
    UnknownOrderAliasError,
    UnknownTimezoneError,
    UnknownWindowSourceError,
    UnsupportedAggregateOperationError,
    WindowFrameOffsetError,
    WindowFrameOrderError,
    WindowOrderingRequiredError,
    WindowSourceMissingError,
)
from public_api.aggregations.executor import build_aggregate_queryset, execute_plan
from public_api.aggregations.fields import (
    AGGREGATE_ATTRIBUTE_NAME_BY_ENTITY,
    AGGREGATE_FIELD_NAME_BY_ENTITY,
    AGGREGATE_RESOURCE_BY_ENTITY,
    AGGREGATE_ROW_TYPE_BY_ENTITY,
    FILTER_INPUT_TYPE_BY_ENTITY,
    aggregate_field,
    aggregate_statement_timeout,
    entity_class_prefix,
)
from public_api.aggregations.filters import (
    AggregateFilterValidationError,
    AppointmentTypeAggregateFilterInput,
    AvailableTimeAggregateFilterInput,
    BlockedTimeAggregateFilterInput,
    CalendarAggregateFilterInput,
    CalendarEventAggregateFilterInput,
    CalendarPoolAggregateFilterInput,
)
from public_api.aggregations.having import (
    HAVING_INPUT_TYPE_BY_ENTITY,
    BooleanAggregateComparison,
    DateTimeAggregateComparison,
    DateTimeComparison,
    FloatComparison,
    IntComparison,
    NumericAggregateComparison,
    StringAggregateComparison,
    StringComparison,
    having_from_input,
    having_input_type,
)
from public_api.aggregations.ordering import (
    METRIC_REF_ENUM_BY_ENTITY,
    METRIC_SPEC_BY_ALIAS_BY_ENTITY,
    ORDER_INPUT_TYPE_BY_ENTITY,
    ORDER_KEY_ENUM_BY_ENTITY,
    OrderDirection,
    order_from_input,
    order_input_type,
)
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
    WindowFunction,
    WindowMetricSpec,
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
from public_api.aggregations.timezone import resolve_timezone
from public_api.aggregations.types import (
    DEFAULT_CONCAT_SEPARATOR,
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
    TemporalGranularity,
)
from public_api.aggregations.windows import (
    WINDOW_INPUT_TYPE_BY_ENTITY,
    WINDOW_METRIC_DEFINITIONS_BY_ENTITY,
    WINDOW_METRICS_TYPE_BY_ENTITY,
    WINDOW_ORDER_INPUT_TYPE_BY_ENTITY,
    WindowBound,
    WindowFrameInput,
    WindowFrameType,
    build_window_metrics,
    window_from_input,
    window_input_type,
    window_metrics_type,
)


__all__ = [
    "AGGREGATE_ATTRIBUTE_NAME_BY_ENTITY",
    "AGGREGATE_FIELD_NAME_BY_ENTITY",
    "AGGREGATE_RESOURCE_BY_ENTITY",
    "AGGREGATE_ROW_TYPE_BY_ENTITY",
    "DEFAULT_CONCAT_SEPARATOR",
    "DEFAULT_METRIC_OPTIONS",
    "FILTER_INPUT_TYPE_BY_ENTITY",
    "GROUP_BY_INPUT_TYPE_BY_ENTITY",
    "GROUP_KEY_TYPE_BY_ENTITY",
    "HAVING_INPUT_TYPE_BY_ENTITY",
    "MAX_LIMIT",
    "METRIC_REF_ENUM_BY_ENTITY",
    "METRIC_SPEC_BY_ALIAS_BY_ENTITY",
    "MIN_LIMIT",
    "ORDER_INPUT_TYPE_BY_ENTITY",
    "ORDER_KEY_ENUM_BY_ENTITY",
    "REGISTRY",
    "WINDOW_INPUT_TYPE_BY_ENTITY",
    "WINDOW_METRICS_TYPE_BY_ENTITY",
    "WINDOW_METRIC_DEFINITIONS_BY_ENTITY",
    "WINDOW_ORDER_INPUT_TYPE_BY_ENTITY",
    "AggregatableEntity",
    "AggregatableField",
    "AggregateConfigurationError",
    "AggregateError",
    "AggregateFilterValidationError",
    "AggregateKind",
    "AggregateOp",
    "AggregateQueryPlan",
    "AggregateQueryTimeoutError",
    "AggregateRegistrationError",
    "AliasCollisionError",
    "AmbiguousGroupByError",
    "AmbiguousOrderInputError",
    "AppointmentTypeAggregateFilterInput",
    "AppointmentTypeGroupByInput",
    "AppointmentTypeGroupKey",
    "AppointmentTypeScalarGroupByField",
    "AppointmentTypeTemporalGroupBy",
    "AppointmentTypeTemporalGroupByField",
    "AvailableTimeAggregateFilterInput",
    "AvailableTimeGroupByInput",
    "AvailableTimeGroupKey",
    "AvailableTimeScalarGroupByField",
    "AvailableTimeTemporalGroupBy",
    "AvailableTimeTemporalGroupByField",
    "BlockedTimeAggregateFilterInput",
    "BlockedTimeGroupByInput",
    "BlockedTimeGroupKey",
    "BlockedTimeScalarGroupByField",
    "BlockedTimeTemporalGroupBy",
    "BlockedTimeTemporalGroupByField",
    "BooleanAggregate",
    "BooleanAggregateComparison",
    "CalendarAggregateFilterInput",
    "CalendarEventAggregateFilterInput",
    "CalendarEventGroupByInput",
    "CalendarEventGroupKey",
    "CalendarEventScalarGroupByField",
    "CalendarEventTemporalGroupBy",
    "CalendarEventTemporalGroupByField",
    "CalendarGroupByInput",
    "CalendarGroupKey",
    "CalendarPoolAggregateFilterInput",
    "CalendarPoolGroupByInput",
    "CalendarPoolGroupKey",
    "CalendarPoolTemporalGroupBy",
    "CalendarPoolTemporalGroupByField",
    "CalendarScalarGroupByField",
    "CalendarTemporalGroupBy",
    "CalendarTemporalGroupByField",
    "ConcatArgumentsMismatchError",
    "DateRangeTooLargeError",
    "DateTimeAggregate",
    "DateTimeAggregateComparison",
    "DateTimeComparison",
    "DimensionSpec",
    "DuplicateGroupKeyFieldError",
    "EmptyAggregatePlanError",
    "EntityQuerysetMismatchError",
    "EntityRegistration",
    "FilterBounds",
    "FloatComparison",
    "GroupableField",
    "HavingSpec",
    "IntComparison",
    "LimitOutOfRangeError",
    "MetricSpec",
    "MisplacedFrameBoundError",
    "MissingBucketTimezoneError",
    "NumericAggregate",
    "NumericAggregateComparison",
    "OffsetNegativeError",
    "OrderDirection",
    "OrderSpec",
    "RelationCount",
    "ReservedAliasError",
    "StringAggregate",
    "StringAggregateComparison",
    "StringComparison",
    "TemporalGranularity",
    "UngroupedOrderKeyError",
    "UngroupedPartitionKeyError",
    "UnknownAggregateEntityError",
    "UnknownAggregateFieldError",
    "UnknownDimensionError",
    "UnknownHavingAliasError",
    "UnknownOrderAliasError",
    "UnknownTimezoneError",
    "UnknownWindowSourceError",
    "UnsupportedAggregateOperationError",
    "WindowBound",
    "WindowFrameInput",
    "WindowFrameOffsetError",
    "WindowFrameOrderError",
    "WindowFrameType",
    "WindowFunction",
    "WindowMetricSpec",
    "WindowOrderingRequiredError",
    "WindowSourceMissingError",
    "WindowSpec",
    "aggregate_field",
    "aggregate_kind_for_model_field",
    "aggregate_statement_timeout",
    "build_aggregate_queryset",
    "build_group_key",
    "build_window_metrics",
    "concrete_output_field",
    "dimension_alias",
    "dimensions_from_group_by",
    "entity_class_prefix",
    "execute_plan",
    "get_registration",
    "having_from_input",
    "having_input_type",
    "metric_alias",
    "order_from_input",
    "order_input_type",
    "resolve_timezone",
    "validate_plan",
    "window_from_input",
    "window_input_type",
    "window_metrics_type",
]
