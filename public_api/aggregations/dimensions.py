"""The GraphQL surface of a group-by: what a caller may group on, and the key
type each grouped row comes back with.

Two shapes per entity, not one:

* a **scalar** variant naming a field that has no time in it, and
* a **temporal** variant naming a date-time field and carrying the granularity
  it is bucketed at.

Splitting them is the point. With one input carrying an optional
``granularity``, ``{field: PROVIDER, granularity: DAY}`` is a document the
schema accepts and a resolver has to refuse; with two, the temporal enum has no
``PROVIDER`` member and the scalar variant has no ``granularity`` argument, so
the same mistake fails GraphQL validation before a resolver runs.

``granularity`` is required on the temporal variant. Grouping on a raw timestamp
is legal in the engine but not offered here: a query bounded to a year of events
has at most 366 daily buckets and as many distinct instants as it has rows, and
group cardinality is one of the four things this feature is supposed to bound.

The ``*GroupKey`` types mirror the registry: one nullable field per groupable
dimension, populated only for the dimensions the query named. They are written
out rather than generated so they read like the rest of this project's GraphQL
types; ``test_dimensions.py`` asserts each one still matches the registry.
"""

import datetime
import enum
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from zoneinfo import ZoneInfo

import strawberry

from public_api.aggregations.errors import (
    DUPLICATE_ALIAS_MESSAGE,
    InvalidAggregatePlanError,
)
from public_api.aggregations.plan import AggregatableEntity, DimensionSpec
from public_api.aggregations.registry import build_dimension, get_registration
from public_api.aggregations.types import TemporalGranularity


# ---------------------------------------------------------------------------
# Per-entity group-by field enums
# ---------------------------------------------------------------------------


@strawberry.enum(description="Non-temporal fields a calendar event aggregate can group by.")
class CalendarEventScalarGroupByField(enum.Enum):
    CALENDAR_ID = "calendar_id"
    APPOINTMENT_TYPE_ID = "appointment_type_id"
    BUNDLE_CALENDAR_ID = "bundle_calendar_id"
    IS_BUNDLE_PRIMARY = "is_bundle_primary"
    TIMEZONE = "timezone"
    IS_RECURRING_EXCEPTION = "is_recurring_exception"


@strawberry.enum(description="Date-time fields a calendar event aggregate can bucket by.")
class CalendarEventTemporalGroupByField(enum.Enum):
    START_TIME = "start_time"
    END_TIME = "end_time"
    CREATED = "created"


@strawberry.enum(description="Non-temporal fields an available time aggregate can group by.")
class AvailableTimeScalarGroupByField(enum.Enum):
    CALENDAR_ID = "calendar_id"
    APPOINTMENT_TYPE_SLOT_ID = "appointment_type_slot_id"
    TIMEZONE = "timezone"
    IS_RECURRING_EXCEPTION = "is_recurring_exception"


@strawberry.enum(description="Date-time fields an available time aggregate can bucket by.")
class AvailableTimeTemporalGroupByField(enum.Enum):
    START_TIME = "start_time"
    END_TIME = "end_time"
    CREATED = "created"


@strawberry.enum(description="Non-temporal fields a blocked time aggregate can group by.")
class BlockedTimeScalarGroupByField(enum.Enum):
    CALENDAR_ID = "calendar_id"
    BUNDLE_CALENDAR_ID = "bundle_calendar_id"
    APPOINTMENT_TYPE_SLOT_ID = "appointment_type_slot_id"
    TIMEZONE = "timezone"
    IS_RECURRING_EXCEPTION = "is_recurring_exception"


@strawberry.enum(description="Date-time fields a blocked time aggregate can bucket by.")
class BlockedTimeTemporalGroupByField(enum.Enum):
    START_TIME = "start_time"
    END_TIME = "end_time"
    CREATED = "created"


@strawberry.enum(description="Non-temporal fields an appointment type aggregate can group by.")
class AppointmentTypeScalarGroupByField(enum.Enum):
    ACCEPTS_PUBLIC_SCHEDULING = "accepts_public_scheduling"


@strawberry.enum(description="Date-time fields an appointment type aggregate can bucket by.")
class AppointmentTypeTemporalGroupByField(enum.Enum):
    CREATED = "created"


@strawberry.enum(description="Non-temporal fields a calendar aggregate can group by.")
class CalendarScalarGroupByField(enum.Enum):
    PROVIDER = "provider"
    CALENDAR_TYPE = "calendar_type"
    VISIBILITY = "visibility"
    SYNC_ENABLED = "sync_enabled"
    MANAGE_AVAILABLE_WINDOWS = "manage_available_windows"
    ACCEPTS_PUBLIC_SCHEDULING = "accepts_public_scheduling"


@strawberry.enum(description="Date-time fields a calendar aggregate can bucket by.")
class CalendarTemporalGroupByField(enum.Enum):
    CREATED = "created"


@strawberry.enum(description="Date-time fields a calendar pool aggregate can bucket by.")
class CalendarPoolTemporalGroupByField(enum.Enum):
    CREATED = "created"


# ---------------------------------------------------------------------------
# Per-entity group-by inputs
# ---------------------------------------------------------------------------


@strawberry.input(description="Bucket a calendar event aggregate on a date-time field.")
class CalendarEventTemporalGroupBy:
    field: CalendarEventTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description="One group-by dimension. Set exactly one of `scalar` and `temporal`.")
class CalendarEventGroupByInput:
    scalar: CalendarEventScalarGroupByField | None = None
    temporal: CalendarEventTemporalGroupBy | None = None


@strawberry.input(description="Bucket an available time aggregate on a date-time field.")
class AvailableTimeTemporalGroupBy:
    field: AvailableTimeTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description="One group-by dimension. Set exactly one of `scalar` and `temporal`.")
class AvailableTimeGroupByInput:
    scalar: AvailableTimeScalarGroupByField | None = None
    temporal: AvailableTimeTemporalGroupBy | None = None


@strawberry.input(description="Bucket a blocked time aggregate on a date-time field.")
class BlockedTimeTemporalGroupBy:
    field: BlockedTimeTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description="One group-by dimension. Set exactly one of `scalar` and `temporal`.")
class BlockedTimeGroupByInput:
    scalar: BlockedTimeScalarGroupByField | None = None
    temporal: BlockedTimeTemporalGroupBy | None = None


@strawberry.input(description="Bucket an appointment type aggregate on a date-time field.")
class AppointmentTypeTemporalGroupBy:
    field: AppointmentTypeTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description="One group-by dimension. Set exactly one of `scalar` and `temporal`.")
class AppointmentTypeGroupByInput:
    scalar: AppointmentTypeScalarGroupByField | None = None
    temporal: AppointmentTypeTemporalGroupBy | None = None


@strawberry.input(description="Bucket a calendar aggregate on a date-time field.")
class CalendarTemporalGroupBy:
    field: CalendarTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description="One group-by dimension. Set exactly one of `scalar` and `temporal`.")
class CalendarGroupByInput:
    scalar: CalendarScalarGroupByField | None = None
    temporal: CalendarTemporalGroupBy | None = None


@strawberry.input(description="Bucket a calendar pool aggregate on a date-time field.")
class CalendarPoolTemporalGroupBy:
    field: CalendarPoolTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(
    description=(
        "One group-by dimension. A calendar pool has no categorical column worth "
        "grouping on, so `temporal` is the only variant and it is required."
    )
)
class CalendarPoolGroupByInput:
    temporal: CalendarPoolTemporalGroupBy | None = None


# ---------------------------------------------------------------------------
# Per-entity group key output types
# ---------------------------------------------------------------------------


@strawberry.type(description="The group key of one calendar event aggregate row.")
class CalendarEventGroupKey:
    calendar_id: int | None = None
    appointment_type_id: int | None = None
    bundle_calendar_id: int | None = None
    is_bundle_primary: bool | None = None
    timezone: str | None = None
    is_recurring_exception: bool | None = None
    start_time: datetime.datetime | None = None
    end_time: datetime.datetime | None = None
    created: datetime.datetime | None = None


@strawberry.type(description="The group key of one available time aggregate row.")
class AvailableTimeGroupKey:
    calendar_id: int | None = None
    appointment_type_slot_id: int | None = None
    timezone: str | None = None
    is_recurring_exception: bool | None = None
    start_time: datetime.datetime | None = None
    end_time: datetime.datetime | None = None
    created: datetime.datetime | None = None


@strawberry.type(description="The group key of one blocked time aggregate row.")
class BlockedTimeGroupKey:
    calendar_id: int | None = None
    bundle_calendar_id: int | None = None
    appointment_type_slot_id: int | None = None
    timezone: str | None = None
    is_recurring_exception: bool | None = None
    start_time: datetime.datetime | None = None
    end_time: datetime.datetime | None = None
    created: datetime.datetime | None = None


@strawberry.type(description="The group key of one appointment type aggregate row.")
class AppointmentTypeGroupKey:
    accepts_public_scheduling: bool | None = None
    created: datetime.datetime | None = None


@strawberry.type(description="The group key of one calendar aggregate row.")
class CalendarGroupKey:
    provider: str | None = None
    calendar_type: str | None = None
    visibility: str | None = None
    sync_enabled: bool | None = None
    manage_available_windows: bool | None = None
    accepts_public_scheduling: bool | None = None
    created: datetime.datetime | None = None


@strawberry.type(description="The group key of one calendar pool aggregate row.")
class CalendarPoolGroupKey:
    created: datetime.datetime | None = None


# ---------------------------------------------------------------------------
# Registry-side lookups
# ---------------------------------------------------------------------------

#: The group-by input type each entity's aggregate field accepts.
GROUP_BY_INPUT_TYPES: Mapping[AggregatableEntity, type] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: CalendarEventGroupByInput,
        AggregatableEntity.AVAILABLE_TIME: AvailableTimeGroupByInput,
        AggregatableEntity.BLOCKED_TIME: BlockedTimeGroupByInput,
        AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeGroupByInput,
        AggregatableEntity.CALENDAR: CalendarGroupByInput,
        AggregatableEntity.CALENDAR_POOL: CalendarPoolGroupByInput,
    }
)

#: The group key type each entity's aggregate rows carry.
GROUP_KEY_TYPES: Mapping[AggregatableEntity, type] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: CalendarEventGroupKey,
        AggregatableEntity.AVAILABLE_TIME: AvailableTimeGroupKey,
        AggregatableEntity.BLOCKED_TIME: BlockedTimeGroupKey,
        AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeGroupKey,
        AggregatableEntity.CALENDAR: CalendarGroupKey,
        AggregatableEntity.CALENDAR_POOL: CalendarPoolGroupKey,
    }
)

#: The scalar group-by enum each entity offers. ``CALENDAR_POOL`` is absent on
#: purpose: it has no categorical column, and GraphQL has no empty enum.
SCALAR_GROUP_BY_ENUMS: Mapping[AggregatableEntity, type[enum.Enum]] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: CalendarEventScalarGroupByField,
        AggregatableEntity.AVAILABLE_TIME: AvailableTimeScalarGroupByField,
        AggregatableEntity.BLOCKED_TIME: BlockedTimeScalarGroupByField,
        AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeScalarGroupByField,
        AggregatableEntity.CALENDAR: CalendarScalarGroupByField,
    }
)

#: The temporal group-by enum each entity offers.
TEMPORAL_GROUP_BY_ENUMS: Mapping[AggregatableEntity, type[enum.Enum]] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: CalendarEventTemporalGroupByField,
        AggregatableEntity.AVAILABLE_TIME: AvailableTimeTemporalGroupByField,
        AggregatableEntity.BLOCKED_TIME: BlockedTimeTemporalGroupByField,
        AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeTemporalGroupByField,
        AggregatableEntity.CALENDAR: CalendarTemporalGroupByField,
        AggregatableEntity.CALENDAR_POOL: CalendarPoolTemporalGroupByField,
    }
)


type AnyGroupByInput = (
    CalendarEventGroupByInput
    | AvailableTimeGroupByInput
    | BlockedTimeGroupByInput
    | AppointmentTypeGroupByInput
    | CalendarGroupByInput
    | CalendarPoolGroupByInput
)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

GROUP_BY_VARIANT_MESSAGE = "A group-by dimension must set exactly one of scalar and temporal"
NO_GROUP_BY_MESSAGE = "An aggregate query must group by at least one dimension"


@dataclass(frozen=True, slots=True)
class ResolvedGroupBy:
    """One group-by input turned into a plan dimension, plus where it lands.

    ``key_field`` is the attribute on the entity's ``*GroupKey`` type, which is
    always the registry name. ``dimension.alias`` is the key in the row dict the
    executor returns, and for a bucketed dimension the two differ — see
    :func:`public_api.aggregations.registry.build_dimension` for why.
    """

    dimension: DimensionSpec
    key_field: str


def resolve_group_by(
    entity: AggregatableEntity,
    group_by: AnyGroupByInput,
    tzinfo: ZoneInfo,
) -> ResolvedGroupBy:
    """Turn one group-by input into a dimension, validating the variant.

    ``tzinfo`` is the query's single bucketing clock. It is ignored by a scalar
    dimension and required by a temporal one.
    """
    # ``getattr`` rather than attribute access: a calendar pool's input has no
    # ``scalar`` field at all, because it has nothing categorical to group on.
    scalar = getattr(group_by, "scalar", None)
    temporal = getattr(group_by, "temporal", None)
    if scalar is not None and temporal is not None:
        raise InvalidAggregatePlanError(GROUP_BY_VARIANT_MESSAGE)

    if temporal is not None:
        field_name = str(temporal.field.value)
        return ResolvedGroupBy(
            dimension=build_dimension(
                entity,
                field_name,
                granularity=temporal.granularity,
                tzinfo=tzinfo,
            ),
            key_field=field_name,
        )

    if scalar is not None:
        field_name = str(scalar.value)
        return ResolvedGroupBy(
            dimension=build_dimension(entity, field_name),
            key_field=field_name,
        )

    raise InvalidAggregatePlanError(GROUP_BY_VARIANT_MESSAGE)


def resolve_group_by_inputs(
    entity: AggregatableEntity,
    group_by: Sequence[AnyGroupByInput],
    tzinfo: ZoneInfo,
) -> tuple[ResolvedGroupBy, ...]:
    """Resolve every group-by input a field was given, in the order given.

    Order matters: it is the ``GROUP BY`` order and, absent an explicit
    ``orderBy``, the row order too.
    """
    if not group_by:
        raise InvalidAggregatePlanError(NO_GROUP_BY_MESSAGE)

    resolved = tuple(resolve_group_by(entity, one, tzinfo) for one in group_by)
    key_fields = [one.key_field for one in resolved]
    if len(set(key_fields)) != len(key_fields):
        # Two dimensions on the same field would collide in the group key, and
        # the one that lost would be whichever was written second.
        raise InvalidAggregatePlanError(DUPLICATE_ALIAS_MESSAGE)
    return resolved


def build_group_key(
    entity: AggregatableEntity,
    resolved: Iterable[ResolvedGroupBy],
    row: Mapping[str, object],
) -> object:
    """Build the entity's ``*GroupKey`` from one result row.

    Only the dimensions the query named are passed; every other field on the key
    type keeps its ``None`` default, which is what makes an unrequested
    dimension null rather than absent.
    """
    key_type = GROUP_KEY_TYPES[entity]
    return key_type(**{one.key_field: row[one.dimension.alias] for one in resolved})


def group_key_field_names(entity: AggregatableEntity) -> tuple[str, ...]:
    """Every field the entity's group key type carries, registry order."""
    return tuple(get_registration(entity).groupable)
