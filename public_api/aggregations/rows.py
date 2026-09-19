"""One ``*AggregateRow`` type per entity — the shape a grouped row comes back as.

A row carries its group key, the group's own ``count``, and one field per
aggregatable model field typed by that field's kind. The kind is what makes
``title { sum }`` impossible to write: ``title`` is a ``StringAggregate`` and a
``StringAggregate`` has no ``sum``. Nothing about that decision lives in a
resolver.

Relation counts are exposed as ``<relation>Count`` integers — ``eventsCount``
rather than ``events`` — so a calendar's row cannot be misread as carrying the
events themselves. The suffix is a rule rather than a table, so there is nothing
to drift; ``test_field_registration.py`` checks every row type against the
registry.

Every aggregate field is nullable. An aggregate the document did not select is
never computed, so it comes back null rather than as a zero the caller might
believe.
"""

from collections.abc import Mapping
from types import MappingProxyType

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


def relation_count_field_name(relation_name: str) -> str:
    """The row field a registered relation count is exposed under."""
    return f"{relation_name}_count"


@strawberry.type(description="One grouped row of a calendar event aggregate.")
class CalendarEventAggregateRow:
    key: CalendarEventGroupKey
    count: int = 0
    title: StringAggregate | None = None
    description: StringAggregate | None = None
    timezone: StringAggregate | None = None
    is_bundle_primary: BooleanAggregate | None = None
    is_recurring_exception: BooleanAggregate | None = None
    duration_minutes: NumericAggregate | None = None
    start_time: DateTimeAggregate | None = None
    end_time: DateTimeAggregate | None = None
    created: DateTimeAggregate | None = None
    modified: DateTimeAggregate | None = None
    attendances_count: int | None = None
    external_attendances_count: int | None = None
    resource_allocations_count: int | None = None


@strawberry.type(description="One grouped row of an available time aggregate.")
class AvailableTimeAggregateRow:
    key: AvailableTimeGroupKey
    count: int = 0
    timezone: StringAggregate | None = None
    is_recurring_exception: BooleanAggregate | None = None
    duration_minutes: NumericAggregate | None = None
    start_time: DateTimeAggregate | None = None
    end_time: DateTimeAggregate | None = None
    created: DateTimeAggregate | None = None
    modified: DateTimeAggregate | None = None


@strawberry.type(description="One grouped row of a blocked time aggregate.")
class BlockedTimeAggregateRow:
    key: BlockedTimeGroupKey
    count: int = 0
    reason: StringAggregate | None = None
    timezone: StringAggregate | None = None
    is_recurring_exception: BooleanAggregate | None = None
    duration_minutes: NumericAggregate | None = None
    start_time: DateTimeAggregate | None = None
    end_time: DateTimeAggregate | None = None
    created: DateTimeAggregate | None = None
    modified: DateTimeAggregate | None = None


@strawberry.type(description="One grouped row of an appointment type aggregate.")
class AppointmentTypeAggregateRow:
    key: AppointmentTypeGroupKey
    count: int = 0
    name: StringAggregate | None = None
    description: StringAggregate | None = None
    accepts_public_scheduling: BooleanAggregate | None = None
    duration_minutes: NumericAggregate | None = None
    created: DateTimeAggregate | None = None
    modified: DateTimeAggregate | None = None
    events_count: int | None = None
    slots_count: int | None = None


@strawberry.type(description="One grouped row of a calendar aggregate.")
class CalendarAggregateRow:
    key: CalendarGroupKey
    count: int = 0
    name: StringAggregate | None = None
    description: StringAggregate | None = None
    email: StringAggregate | None = None
    provider: StringAggregate | None = None
    calendar_type: StringAggregate | None = None
    visibility: StringAggregate | None = None
    capacity: NumericAggregate | None = None
    sync_enabled: BooleanAggregate | None = None
    manage_available_windows: BooleanAggregate | None = None
    accepts_public_scheduling: BooleanAggregate | None = None
    created: DateTimeAggregate | None = None
    modified: DateTimeAggregate | None = None
    events_count: int | None = None
    blocked_times_count: int | None = None
    available_times_count: int | None = None


@strawberry.type(description="One grouped row of a calendar pool aggregate.")
class CalendarPoolAggregateRow:
    key: CalendarPoolGroupKey
    count: int = 0
    name: StringAggregate | None = None
    description: StringAggregate | None = None
    created: DateTimeAggregate | None = None
    modified: DateTimeAggregate | None = None
    memberships_count: int | None = None


#: The row type each entity's aggregate field returns.
AGGREGATE_ROW_TYPES: Mapping[AggregatableEntity, type] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: CalendarEventAggregateRow,
        AggregatableEntity.AVAILABLE_TIME: AvailableTimeAggregateRow,
        AggregatableEntity.BLOCKED_TIME: BlockedTimeAggregateRow,
        AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeAggregateRow,
        AggregatableEntity.CALENDAR: CalendarAggregateRow,
        AggregatableEntity.CALENDAR_POOL: CalendarPoolAggregateRow,
    }
)
