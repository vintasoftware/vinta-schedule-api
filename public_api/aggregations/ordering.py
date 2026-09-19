"""Ordering grouped rows by a group key or by a metric.

An order-by names exactly one of the two — a dimension the query grouped on, or
a metric it can reference. Two enums rather than one free-text field, so
``orderBy: {metric: COUNT}`` and ``orderBy: {key: CALENDAR_ID}`` are both
checked by the schema, and "exactly one" is the only thing left for a resolver
to say.

The referenceable metrics are ``registry.referenceable_metrics`` — the same set
``having.py`` filters on, so anything you can sort by you can also threshold.

**The group key is always appended as a tiebreak.** ``ORDER BY count DESC LIMIT
3`` over groups that tie on count is free to return a different three on every
call, and a caller paging through would see rows twice and miss others. Adding
the key makes the order total.
"""

import enum
from collections.abc import Mapping, Sequence

import strawberry

from public_api.aggregations.dimensions import ResolvedGroupBy
from public_api.aggregations.errors import (
    ORDER_NEEDS_EXACTLY_ONE_TARGET_MESSAGE,
    InvalidAggregatePlanError,
    order_by_ungrouped_dimension_message,
)
from public_api.aggregations.plan import AggregatableEntity, MetricSpec, OrderSpec
from public_api.aggregations.registry import MetricReference, referenceable_metrics


@strawberry.enum(description="Sort direction for one order-by entry.")
class OrderDirection(enum.Enum):
    ASC = "ASC"
    DESC = "DESC"


# ---------------------------------------------------------------------------
# Per-entity orderable key enums — every dimension the entity can group by
# ---------------------------------------------------------------------------


@strawberry.enum(description="Group key a calendar event aggregate can be ordered by.")
class CalendarEventOrderableKey(enum.Enum):
    CALENDAR_ID = "calendar_id"
    APPOINTMENT_TYPE_ID = "appointment_type_id"
    BUNDLE_CALENDAR_ID = "bundle_calendar_id"
    IS_BUNDLE_PRIMARY = "is_bundle_primary"
    TIMEZONE = "timezone"
    IS_RECURRING_EXCEPTION = "is_recurring_exception"
    START_TIME = "start_time"
    END_TIME = "end_time"
    CREATED = "created"


@strawberry.enum(description="Group key an available time aggregate can be ordered by.")
class AvailableTimeOrderableKey(enum.Enum):
    CALENDAR_ID = "calendar_id"
    APPOINTMENT_TYPE_SLOT_ID = "appointment_type_slot_id"
    TIMEZONE = "timezone"
    IS_RECURRING_EXCEPTION = "is_recurring_exception"
    START_TIME = "start_time"
    END_TIME = "end_time"
    CREATED = "created"


@strawberry.enum(description="Group key a blocked time aggregate can be ordered by.")
class BlockedTimeOrderableKey(enum.Enum):
    CALENDAR_ID = "calendar_id"
    BUNDLE_CALENDAR_ID = "bundle_calendar_id"
    APPOINTMENT_TYPE_SLOT_ID = "appointment_type_slot_id"
    TIMEZONE = "timezone"
    IS_RECURRING_EXCEPTION = "is_recurring_exception"
    START_TIME = "start_time"
    END_TIME = "end_time"
    CREATED = "created"


@strawberry.enum(description="Group key an appointment type aggregate can be ordered by.")
class AppointmentTypeOrderableKey(enum.Enum):
    ACCEPTS_PUBLIC_SCHEDULING = "accepts_public_scheduling"
    CREATED = "created"


@strawberry.enum(description="Group key a calendar aggregate can be ordered by.")
class CalendarOrderableKey(enum.Enum):
    PROVIDER = "provider"
    CALENDAR_TYPE = "calendar_type"
    VISIBILITY = "visibility"
    SYNC_ENABLED = "sync_enabled"
    MANAGE_AVAILABLE_WINDOWS = "manage_available_windows"
    ACCEPTS_PUBLIC_SCHEDULING = "accepts_public_scheduling"
    CREATED = "created"


@strawberry.enum(description="Group key a calendar pool aggregate can be ordered by.")
class CalendarPoolOrderableKey(enum.Enum):
    CREATED = "created"


# ---------------------------------------------------------------------------
# Per-entity metric references
# ---------------------------------------------------------------------------


@strawberry.enum(description="Metric a calendar event aggregate can be ordered by.")
class CalendarEventMetricRef(enum.Enum):
    COUNT = "count"
    DURATION_MINUTES_SUM = "duration_minutes_sum"
    DURATION_MINUTES_AVG = "duration_minutes_avg"
    DURATION_MINUTES_MIN = "duration_minutes_min"
    DURATION_MINUTES_MAX = "duration_minutes_max"
    ATTENDANCES_COUNT = "attendances_count"
    EXTERNAL_ATTENDANCES_COUNT = "external_attendances_count"
    RESOURCE_ALLOCATIONS_COUNT = "resource_allocations_count"


@strawberry.enum(description="Metric an available time aggregate can be ordered by.")
class AvailableTimeMetricRef(enum.Enum):
    COUNT = "count"
    DURATION_MINUTES_SUM = "duration_minutes_sum"
    DURATION_MINUTES_AVG = "duration_minutes_avg"
    DURATION_MINUTES_MIN = "duration_minutes_min"
    DURATION_MINUTES_MAX = "duration_minutes_max"


@strawberry.enum(description="Metric a blocked time aggregate can be ordered by.")
class BlockedTimeMetricRef(enum.Enum):
    COUNT = "count"
    DURATION_MINUTES_SUM = "duration_minutes_sum"
    DURATION_MINUTES_AVG = "duration_minutes_avg"
    DURATION_MINUTES_MIN = "duration_minutes_min"
    DURATION_MINUTES_MAX = "duration_minutes_max"


@strawberry.enum(description="Metric an appointment type aggregate can be ordered by.")
class AppointmentTypeMetricRef(enum.Enum):
    COUNT = "count"
    DURATION_MINUTES_SUM = "duration_minutes_sum"
    DURATION_MINUTES_AVG = "duration_minutes_avg"
    DURATION_MINUTES_MIN = "duration_minutes_min"
    DURATION_MINUTES_MAX = "duration_minutes_max"
    EVENTS_COUNT = "events_count"
    SLOTS_COUNT = "slots_count"


@strawberry.enum(description="Metric a calendar aggregate can be ordered by.")
class CalendarMetricRef(enum.Enum):
    COUNT = "count"
    CAPACITY_SUM = "capacity_sum"
    CAPACITY_AVG = "capacity_avg"
    CAPACITY_MIN = "capacity_min"
    CAPACITY_MAX = "capacity_max"
    EVENTS_COUNT = "events_count"
    BLOCKED_TIMES_COUNT = "blocked_times_count"
    AVAILABLE_TIMES_COUNT = "available_times_count"


@strawberry.enum(description="Metric a calendar pool aggregate can be ordered by.")
class CalendarPoolMetricRef(enum.Enum):
    COUNT = "count"
    MEMBERSHIPS_COUNT = "memberships_count"


# ---------------------------------------------------------------------------
# Per-entity order inputs
# ---------------------------------------------------------------------------

_ORDER_DESCRIPTION = (
    "One ordering. Set exactly one of `key` and `metric`. The group key is "
    "always appended as a tiebreak, so paging stays stable across calls."
)


@strawberry.input(description=_ORDER_DESCRIPTION)
class CalendarEventAggregateOrderInput:
    key: CalendarEventOrderableKey | None = None
    metric: CalendarEventMetricRef | None = None
    direction: OrderDirection = OrderDirection.DESC


@strawberry.input(description=_ORDER_DESCRIPTION)
class AvailableTimeAggregateOrderInput:
    key: AvailableTimeOrderableKey | None = None
    metric: AvailableTimeMetricRef | None = None
    direction: OrderDirection = OrderDirection.DESC


@strawberry.input(description=_ORDER_DESCRIPTION)
class BlockedTimeAggregateOrderInput:
    key: BlockedTimeOrderableKey | None = None
    metric: BlockedTimeMetricRef | None = None
    direction: OrderDirection = OrderDirection.DESC


@strawberry.input(description=_ORDER_DESCRIPTION)
class AppointmentTypeAggregateOrderInput:
    key: AppointmentTypeOrderableKey | None = None
    metric: AppointmentTypeMetricRef | None = None
    direction: OrderDirection = OrderDirection.DESC


@strawberry.input(description=_ORDER_DESCRIPTION)
class CalendarAggregateOrderInput:
    key: CalendarOrderableKey | None = None
    metric: CalendarMetricRef | None = None
    direction: OrderDirection = OrderDirection.DESC


@strawberry.input(description=_ORDER_DESCRIPTION)
class CalendarPoolAggregateOrderInput:
    key: CalendarPoolOrderableKey | None = None
    metric: CalendarPoolMetricRef | None = None
    direction: OrderDirection = OrderDirection.DESC


#: The order input each entity's aggregate field accepts.
ORDER_INPUT_TYPES: Mapping[AggregatableEntity, type] = {
    AggregatableEntity.CALENDAR_EVENT: CalendarEventAggregateOrderInput,
    AggregatableEntity.AVAILABLE_TIME: AvailableTimeAggregateOrderInput,
    AggregatableEntity.BLOCKED_TIME: BlockedTimeAggregateOrderInput,
    AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeAggregateOrderInput,
    AggregatableEntity.CALENDAR: CalendarAggregateOrderInput,
    AggregatableEntity.CALENDAR_POOL: CalendarPoolAggregateOrderInput,
}

#: The orderable-key enum each entity offers, for the drift tests.
ORDERABLE_KEY_ENUMS: Mapping[AggregatableEntity, type[enum.Enum]] = {
    AggregatableEntity.CALENDAR_EVENT: CalendarEventOrderableKey,
    AggregatableEntity.AVAILABLE_TIME: AvailableTimeOrderableKey,
    AggregatableEntity.BLOCKED_TIME: BlockedTimeOrderableKey,
    AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeOrderableKey,
    AggregatableEntity.CALENDAR: CalendarOrderableKey,
    AggregatableEntity.CALENDAR_POOL: CalendarPoolOrderableKey,
}

#: The metric-reference enum each entity offers, for the drift tests.
METRIC_REF_ENUMS: Mapping[AggregatableEntity, type[enum.Enum]] = {
    AggregatableEntity.CALENDAR_EVENT: CalendarEventMetricRef,
    AggregatableEntity.AVAILABLE_TIME: AvailableTimeMetricRef,
    AggregatableEntity.BLOCKED_TIME: BlockedTimeMetricRef,
    AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeMetricRef,
    AggregatableEntity.CALENDAR: CalendarMetricRef,
    AggregatableEntity.CALENDAR_POOL: CalendarPoolMetricRef,
}

type AnyOrderInput = (
    CalendarEventAggregateOrderInput
    | AvailableTimeAggregateOrderInput
    | BlockedTimeAggregateOrderInput
    | AppointmentTypeAggregateOrderInput
    | CalendarAggregateOrderInput
    | CalendarPoolAggregateOrderInput
)


def resolve_order_by(
    entity: AggregatableEntity,
    order_by: Sequence[AnyOrderInput] | None,
    resolved_group_by: Sequence[ResolvedGroupBy],
) -> tuple[tuple[OrderSpec, ...], tuple[MetricSpec, ...]]:
    """Turn order inputs into plan specs plus any metric they need annotated.

    Returns ``((), ())`` for an absent or empty list, which leaves the executor
    on its group-key default and keeps a query without ``orderBy`` identical to
    one from before this phase.

    A ``key`` is resolved through the query's own group-by list rather than
    through the registry: a dimension bucketed at ``DAY`` is computed under
    ``start_time_day``, not ``start_time``, and ordering has to name the column
    that exists.
    """
    if not order_by:
        return (), ()

    alias_for_key = {one.key_field: one.dimension.alias for one in resolved_group_by}
    references = referenceable_metrics(entity)

    specs: list[OrderSpec] = []
    required: dict[str, MetricReference] = {}

    for one in order_by:
        key = one.key
        metric = one.metric
        if key is not None and metric is not None:
            raise InvalidAggregatePlanError(ORDER_NEEDS_EXACTLY_ONE_TARGET_MESSAGE)

        descending = one.direction is OrderDirection.DESC
        if key is not None:
            field_name = str(key.value)
            alias = alias_for_key.get(field_name)
            if alias is None:
                raise InvalidAggregatePlanError(order_by_ungrouped_dimension_message(field_name))
            specs.append(OrderSpec(alias=alias, descending=descending))
            continue

        if metric is None:
            raise InvalidAggregatePlanError(ORDER_NEEDS_EXACTLY_ONE_TARGET_MESSAGE)

        reference = references[str(metric.value)]
        required.setdefault(reference.alias, reference)
        specs.append(OrderSpec(alias=reference.alias, descending=descending))

    return tuple(specs), tuple(reference.to_metric() for reference in required.values())
