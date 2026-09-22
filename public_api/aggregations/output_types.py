"""The per-entity ``*AggregateRow`` output types.

One row type per registered entity, mirroring ``registry.py``'s
``EntityRegistration.metrics`` / ``.relation_counts`` field-for-field:

* ``key`` -- the entity's ``*GroupKey`` type from ``dimensions.py``.
* ``count`` -- the row count, always present and non-null.
* one nullable field per aggregatable metric, typed as the
  ``NumericAggregate`` / ``StringAggregate`` / ``DateTimeAggregate`` /
  ``BooleanAggregate`` the registry's ``FieldKind`` maps it to.
* one nullable ``int`` field per relation count.

A field is only populated when the caller's GraphQL selection asked for it
(or for one of its sub-fields) -- see ``public_api.aggregations.fields``,
which reads the selection to decide what to compute and leaves everything
else at its ``None`` default. Attribute names match the registry's field
paths / relation-count keys exactly (``duration_minutes``, not
``durationMinutes``); Strawberry's own camelCase conversion is what turns
that into the GraphQL name a caller reads.
"""

from types import MappingProxyType
from typing import Any

import strawberry

from public_api.aggregations.dimensions import (
    AppointmentTypeGroupKey,
    AvailableTimeGroupKey,
    BlockedTimeGroupKey,
    CalendarEventGroupKey,
    CalendarGroupKey,
    CalendarPoolGroupKey,
)
from public_api.aggregations.plan import AggregatableEntity
from public_api.aggregations.types import (
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
)


_ROW_DESCRIPTION = (
    "One aggregate group. Fields the query's selection did not ask for are "
    "null rather than absent -- the schema shape is the same regardless of "
    "which metrics a particular call requested."
)


@strawberry.type(description=_ROW_DESCRIPTION)
class CalendarEventAggregateRow:
    key: CalendarEventGroupKey
    count: int
    duration_minutes: NumericAggregate | None = None
    title: StringAggregate | None = None
    description: StringAggregate | None = None
    start_time: DateTimeAggregate | None = None
    end_time: DateTimeAggregate | None = None
    created: DateTimeAggregate | None = None
    is_recurring_exception: BooleanAggregate | None = None
    is_bundle_primary: BooleanAggregate | None = None
    attendance_count: int | None = None
    external_attendance_count: int | None = None
    resource_allocation_count: int | None = None


@strawberry.type(description=_ROW_DESCRIPTION)
class AvailableTimeAggregateRow:
    key: AvailableTimeGroupKey
    count: int
    duration_minutes: NumericAggregate | None = None
    start_time: DateTimeAggregate | None = None
    end_time: DateTimeAggregate | None = None
    created: DateTimeAggregate | None = None
    is_recurring_exception: BooleanAggregate | None = None


@strawberry.type(description=_ROW_DESCRIPTION)
class BlockedTimeAggregateRow:
    key: BlockedTimeGroupKey
    count: int
    duration_minutes: NumericAggregate | None = None
    reason: StringAggregate | None = None
    start_time: DateTimeAggregate | None = None
    end_time: DateTimeAggregate | None = None
    created: DateTimeAggregate | None = None
    is_recurring_exception: BooleanAggregate | None = None


@strawberry.type(description=_ROW_DESCRIPTION)
class AppointmentTypeAggregateRow:
    key: AppointmentTypeGroupKey
    count: int
    name: StringAggregate | None = None
    description: StringAggregate | None = None
    created: DateTimeAggregate | None = None
    accepts_public_scheduling: BooleanAggregate | None = None
    duration_minutes: NumericAggregate | None = None
    event_count: int | None = None
    slot_count: int | None = None


@strawberry.type(description=_ROW_DESCRIPTION)
class CalendarAggregateRow:
    key: CalendarGroupKey
    count: int
    name: StringAggregate | None = None
    description: StringAggregate | None = None
    capacity: NumericAggregate | None = None
    created: DateTimeAggregate | None = None
    manage_available_windows: BooleanAggregate | None = None
    accepts_public_scheduling: BooleanAggregate | None = None
    sync_enabled: BooleanAggregate | None = None
    event_count: int | None = None
    blocked_time_count: int | None = None
    available_time_count: int | None = None


@strawberry.type(description=_ROW_DESCRIPTION)
class CalendarPoolAggregateRow:
    key: CalendarPoolGroupKey
    count: int
    name: StringAggregate | None = None
    description: StringAggregate | None = None
    created: DateTimeAggregate | None = None
    calendar_count: int | None = None


#: Per-entity row type, keyed the same way every other per-entity lookup
#: table in this package is (``dimensions.GROUP_KEY_TYPE_BY_ENTITY``,
#: ``registry._REGISTRY``, ...).
ROW_TYPE_BY_ENTITY: MappingProxyType[AggregatableEntity, type[Any]] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: CalendarEventAggregateRow,
        AggregatableEntity.AVAILABLE_TIME: AvailableTimeAggregateRow,
        AggregatableEntity.BLOCKED_TIME: BlockedTimeAggregateRow,
        AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeAggregateRow,
        AggregatableEntity.CALENDAR: CalendarAggregateRow,
        AggregatableEntity.CALENDAR_POOL: CalendarPoolAggregateRow,
    }
)
