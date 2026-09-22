"""What a caller may group by, and what the group key comes back as.

Three things per entity, all keyed off the same registry entry so they
cannot drift apart from each other or from the executor:

* a **scalar** group-by enum, holding every dimension the registry does
  *not* mark temporal;
* a **temporal** group-by enum, holding every dimension it does, paired in
  its own input type with a mandatory ``granularity``;
* a **group key** output type with one nullable field per dimension alias.

The split into two enums is the whole point. GraphQL has no input unions, so
a single ``{field, granularity}`` input would let a caller ask for
``CALENDAR_ID`` bucketed by ``WEEK`` and the refusal would have to come from
a resolver at run time. Two enums make that unrepresentable: ``CALENDAR_ID``
is not a member of the temporal enum, so the query fails GraphQL validation
before anything runs. What is left for run time is only "exactly one of the
two slots", which is the part the type system genuinely cannot state.

**Aliases are derived, never tabled.** A dimension's alias is the key its
value lands under in the row dict *and* the field name on the group key
type, so one rule ties the three together:

* a concrete foreign key column loses its plumbing -- ``calendar_fk_id``
  becomes ``calendar_id``, which is what a partner reads;
* a temporal dimension gains ``_bucket`` -- ``start_time`` becomes
  ``start_time_bucket``. It holds the truncated start of a bucket rather
  than a row's ``start_time``, and Django refuses an annotation named after
  a model field anyway, which ``TruncDay("start_time")`` aliased
  ``start_time`` would be;
* everything else is already its own name.
"""

import dataclasses
import datetime
import enum
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import strawberry

from public_api.aggregations.errors import GroupByVariantError
from public_api.aggregations.plan import AggregatableEntity, DimensionSpec
from public_api.aggregations.timezone import resolve_bucketing_timezone
from public_api.aggregations.types import TemporalGranularity


# ---------------------------------------------------------------------------
# Alias derivation
# ---------------------------------------------------------------------------

#: Suffix an ``OrganizationSafeForeignKey``'s concrete column carries.
_FK_COLUMN_SUFFIX = "_fk_id"

#: Suffix a truncated temporal dimension's alias carries.
_BUCKET_SUFFIX = "_bucket"


def scalar_alias(field_path: str) -> str:
    """The row-dict key a scalar dimension lands under."""
    if field_path.endswith(_FK_COLUMN_SUFFIX):
        return field_path.removesuffix(_FK_COLUMN_SUFFIX) + "_id"
    return field_path


def temporal_alias(field_path: str) -> str:
    """The row-dict key a bucketed temporal dimension lands under."""
    return field_path + _BUCKET_SUFFIX


# ---------------------------------------------------------------------------
# Group-by enums
# ---------------------------------------------------------------------------
#
# Every member's value is the registry field path it names, so the executor
# and the registry never need a translation table. Member *names* are what
# GraphQL publishes.


@strawberry.enum(description="Scalar dimensions a calendar event aggregate may group by.")
class CalendarEventScalarGroupByField(enum.Enum):
    ID = "id"
    CALENDAR_ID = "calendar_fk_id"
    APPOINTMENT_TYPE_ID = "appointment_type_fk_id"
    BUNDLE_CALENDAR_ID = "bundle_calendar_fk_id"
    IS_BUNDLE_PRIMARY = "is_bundle_primary"
    IS_RECURRING_EXCEPTION = "is_recurring_exception"
    TIMEZONE = "timezone"


@strawberry.enum(description="Temporal dimensions a calendar event aggregate may bucket by.")
class CalendarEventTemporalGroupByField(enum.Enum):
    START_TIME = "start_time"
    END_TIME = "end_time"
    CREATED = "created"


@strawberry.enum(description="Scalar dimensions an available time aggregate may group by.")
class AvailableTimeScalarGroupByField(enum.Enum):
    ID = "id"
    CALENDAR_ID = "calendar_fk_id"
    IS_RECURRING_EXCEPTION = "is_recurring_exception"
    TIMEZONE = "timezone"


@strawberry.enum(description="Temporal dimensions an available time aggregate may bucket by.")
class AvailableTimeTemporalGroupByField(enum.Enum):
    START_TIME = "start_time"
    END_TIME = "end_time"
    CREATED = "created"


@strawberry.enum(description="Scalar dimensions a blocked time aggregate may group by.")
class BlockedTimeScalarGroupByField(enum.Enum):
    ID = "id"
    CALENDAR_ID = "calendar_fk_id"
    BUNDLE_CALENDAR_ID = "bundle_calendar_fk_id"
    IS_RECURRING_EXCEPTION = "is_recurring_exception"
    TIMEZONE = "timezone"


@strawberry.enum(description="Temporal dimensions a blocked time aggregate may bucket by.")
class BlockedTimeTemporalGroupByField(enum.Enum):
    START_TIME = "start_time"
    END_TIME = "end_time"
    CREATED = "created"


@strawberry.enum(description="Scalar dimensions an appointment type aggregate may group by.")
class AppointmentTypeScalarGroupByField(enum.Enum):
    ID = "id"
    ACCEPTS_PUBLIC_SCHEDULING = "accepts_public_scheduling"


@strawberry.enum(description="Temporal dimensions an appointment type aggregate may bucket by.")
class AppointmentTypeTemporalGroupByField(enum.Enum):
    CREATED = "created"


@strawberry.enum(description="Scalar dimensions a calendar aggregate may group by.")
class CalendarScalarGroupByField(enum.Enum):
    ID = "id"
    PROVIDER = "provider"
    CALENDAR_TYPE = "calendar_type"
    VISIBILITY = "visibility"
    MANAGE_AVAILABLE_WINDOWS = "manage_available_windows"
    ACCEPTS_PUBLIC_SCHEDULING = "accepts_public_scheduling"
    SYNC_ENABLED = "sync_enabled"


@strawberry.enum(description="Temporal dimensions a calendar aggregate may bucket by.")
class CalendarTemporalGroupByField(enum.Enum):
    CREATED = "created"


@strawberry.enum(description="Scalar dimensions a calendar pool aggregate may group by.")
class CalendarPoolScalarGroupByField(enum.Enum):
    ID = "id"


@strawberry.enum(description="Temporal dimensions a calendar pool aggregate may bucket by.")
class CalendarPoolTemporalGroupByField(enum.Enum):
    CREATED = "created"


# ---------------------------------------------------------------------------
# Group-by inputs
# ---------------------------------------------------------------------------
#
# Two types per entity: the temporal variant, where a granularity is
# mandatory because an untruncated timestamp is a key every row is distinct
# on; and the wrapper, which carries one nullable slot per variant because
# GraphQL cannot express "one of these".

_TEMPORAL_INPUT_DESCRIPTION = (
    "A temporal dimension plus the width of the bucket it is truncated to. "
    "Buckets are measured in the aggregate's timezone argument and are "
    "sparse: a bucket with no matching rows produces no row."
)

_GROUP_BY_INPUT_DESCRIPTION = (
    "One group-by dimension. Set exactly one of 'field' (a scalar "
    "dimension) or 'temporal' (a dimension plus a granularity)."
)


@strawberry.input(description=_TEMPORAL_INPUT_DESCRIPTION)
class CalendarEventTemporalGroupByInput:
    field: CalendarEventTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description=_GROUP_BY_INPUT_DESCRIPTION)
class CalendarEventGroupByInput:
    field: CalendarEventScalarGroupByField | None = None
    temporal: CalendarEventTemporalGroupByInput | None = None


@strawberry.input(description=_TEMPORAL_INPUT_DESCRIPTION)
class AvailableTimeTemporalGroupByInput:
    field: AvailableTimeTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description=_GROUP_BY_INPUT_DESCRIPTION)
class AvailableTimeGroupByInput:
    field: AvailableTimeScalarGroupByField | None = None
    temporal: AvailableTimeTemporalGroupByInput | None = None


@strawberry.input(description=_TEMPORAL_INPUT_DESCRIPTION)
class BlockedTimeTemporalGroupByInput:
    field: BlockedTimeTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description=_GROUP_BY_INPUT_DESCRIPTION)
class BlockedTimeGroupByInput:
    field: BlockedTimeScalarGroupByField | None = None
    temporal: BlockedTimeTemporalGroupByInput | None = None


@strawberry.input(description=_TEMPORAL_INPUT_DESCRIPTION)
class AppointmentTypeTemporalGroupByInput:
    field: AppointmentTypeTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description=_GROUP_BY_INPUT_DESCRIPTION)
class AppointmentTypeGroupByInput:
    field: AppointmentTypeScalarGroupByField | None = None
    temporal: AppointmentTypeTemporalGroupByInput | None = None


@strawberry.input(description=_TEMPORAL_INPUT_DESCRIPTION)
class CalendarTemporalGroupByInput:
    field: CalendarTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description=_GROUP_BY_INPUT_DESCRIPTION)
class CalendarGroupByInput:
    field: CalendarScalarGroupByField | None = None
    temporal: CalendarTemporalGroupByInput | None = None


@strawberry.input(description=_TEMPORAL_INPUT_DESCRIPTION)
class CalendarPoolTemporalGroupByInput:
    field: CalendarPoolTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description=_GROUP_BY_INPUT_DESCRIPTION)
class CalendarPoolGroupByInput:
    field: CalendarPoolScalarGroupByField | None = None
    temporal: CalendarPoolTemporalGroupByInput | None = None


# ---------------------------------------------------------------------------
# Group key output types
# ---------------------------------------------------------------------------
#
# Every field is nullable and defaults to ``None``: a group key carries one
# slot per dimension the entity *could* be grouped by, and only the ones the
# query named come back populated. A caller that grouped by day alone reads
# ``startTimeBucket`` and gets ``null`` everywhere else, which is the honest
# answer -- the query did not ask.

_GROUP_KEY_DESCRIPTION = (
    "The key of one aggregate group. Only the dimensions this query's "
    "groupBy named are populated; the rest are null."
)


@strawberry.type(description=_GROUP_KEY_DESCRIPTION)
class CalendarEventGroupKey:
    id: int | None = None
    calendar_id: int | None = None
    appointment_type_id: int | None = None
    bundle_calendar_id: int | None = None
    is_bundle_primary: bool | None = None
    is_recurring_exception: bool | None = None
    timezone: str | None = None
    start_time_bucket: datetime.datetime | None = None
    end_time_bucket: datetime.datetime | None = None
    created_bucket: datetime.datetime | None = None


@strawberry.type(description=_GROUP_KEY_DESCRIPTION)
class AvailableTimeGroupKey:
    id: int | None = None
    calendar_id: int | None = None
    is_recurring_exception: bool | None = None
    timezone: str | None = None
    start_time_bucket: datetime.datetime | None = None
    end_time_bucket: datetime.datetime | None = None
    created_bucket: datetime.datetime | None = None


@strawberry.type(description=_GROUP_KEY_DESCRIPTION)
class BlockedTimeGroupKey:
    id: int | None = None
    calendar_id: int | None = None
    bundle_calendar_id: int | None = None
    is_recurring_exception: bool | None = None
    timezone: str | None = None
    start_time_bucket: datetime.datetime | None = None
    end_time_bucket: datetime.datetime | None = None
    created_bucket: datetime.datetime | None = None


@strawberry.type(description=_GROUP_KEY_DESCRIPTION)
class AppointmentTypeGroupKey:
    id: int | None = None
    accepts_public_scheduling: bool | None = None
    created_bucket: datetime.datetime | None = None


@strawberry.type(description=_GROUP_KEY_DESCRIPTION)
class CalendarGroupKey:
    id: int | None = None
    provider: str | None = None
    calendar_type: str | None = None
    visibility: str | None = None
    manage_available_windows: bool | None = None
    accepts_public_scheduling: bool | None = None
    sync_enabled: bool | None = None
    created_bucket: datetime.datetime | None = None


@strawberry.type(description=_GROUP_KEY_DESCRIPTION)
class CalendarPoolGroupKey:
    id: int | None = None
    created_bucket: datetime.datetime | None = None


# ---------------------------------------------------------------------------
# Per-entity lookup tables
# ---------------------------------------------------------------------------

SCALAR_GROUP_BY_FIELD_BY_ENTITY: Mapping[AggregatableEntity, type[enum.Enum]] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: CalendarEventScalarGroupByField,
        AggregatableEntity.AVAILABLE_TIME: AvailableTimeScalarGroupByField,
        AggregatableEntity.BLOCKED_TIME: BlockedTimeScalarGroupByField,
        AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeScalarGroupByField,
        AggregatableEntity.CALENDAR: CalendarScalarGroupByField,
        AggregatableEntity.CALENDAR_POOL: CalendarPoolScalarGroupByField,
    }
)

TEMPORAL_GROUP_BY_FIELD_BY_ENTITY: Mapping[AggregatableEntity, type[enum.Enum]] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: CalendarEventTemporalGroupByField,
        AggregatableEntity.AVAILABLE_TIME: AvailableTimeTemporalGroupByField,
        AggregatableEntity.BLOCKED_TIME: BlockedTimeTemporalGroupByField,
        AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeTemporalGroupByField,
        AggregatableEntity.CALENDAR: CalendarTemporalGroupByField,
        AggregatableEntity.CALENDAR_POOL: CalendarPoolTemporalGroupByField,
    }
)

GROUP_BY_INPUT_BY_ENTITY: Mapping[AggregatableEntity, type] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: CalendarEventGroupByInput,
        AggregatableEntity.AVAILABLE_TIME: AvailableTimeGroupByInput,
        AggregatableEntity.BLOCKED_TIME: BlockedTimeGroupByInput,
        AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeGroupByInput,
        AggregatableEntity.CALENDAR: CalendarGroupByInput,
        AggregatableEntity.CALENDAR_POOL: CalendarPoolGroupByInput,
    }
)

GROUP_KEY_TYPE_BY_ENTITY: Mapping[AggregatableEntity, type] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: CalendarEventGroupKey,
        AggregatableEntity.AVAILABLE_TIME: AvailableTimeGroupKey,
        AggregatableEntity.BLOCKED_TIME: BlockedTimeGroupKey,
        AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeGroupKey,
        AggregatableEntity.CALENDAR: CalendarGroupKey,
        AggregatableEntity.CALENDAR_POOL: CalendarPoolGroupKey,
    }
)


# ---------------------------------------------------------------------------
# Input to plan
# ---------------------------------------------------------------------------


class GroupByEntry(Protocol):
    """The shape every entity's group-by wrapper input has.

    Structural rather than a shared base class: each entity's slots are
    typed to its own enums, so there is nothing to inherit but this
    contract.
    """

    field: Any
    temporal: Any


def resolve_dimensions(
    group_by: Sequence[GroupByEntry], timezone_name: str
) -> tuple[DimensionSpec, ...]:
    """Turn a query's ``groupBy`` argument into the plan's dimensions.

    ``timezone_name`` is resolved once, up front, and every temporal
    dimension in the result carries the same :class:`ZoneInfo`. Resolving it
    per entry would let two dimensions of one query bucket against different
    wall clocks, which is the failure the caller-supplied timezone exists to
    prevent. It is validated even when no dimension is temporal, so a bad
    name is refused the same way regardless of what else the query asked
    for.
    """
    tzinfo = resolve_bucketing_timezone(timezone_name)
    return tuple(_dimension_from_group_by(entry, tzinfo) for entry in group_by)


def _dimension_from_group_by(entry: GroupByEntry, tzinfo: ZoneInfo) -> DimensionSpec:
    """One ``groupBy`` entry as a :class:`DimensionSpec`."""
    scalar = entry.field
    temporal = entry.temporal
    if (scalar is None) == (temporal is None):
        raise GroupByVariantError()

    if scalar is not None:
        return DimensionSpec(alias=scalar_alias(scalar.value), field_path=scalar.value)

    field_path = temporal.field.value
    return DimensionSpec(
        alias=temporal_alias(field_path),
        field_path=field_path,
        granularity=temporal.granularity,
        tzinfo=tzinfo,
    )


def build_group_key(entity: AggregatableEntity, row: Mapping[str, Any]) -> Any:
    """Map one result row's dimension values onto the entity's group key type.

    Only keys the type declares are read, so handing it a whole row -- keys,
    metrics and all -- is safe. Dimensions the query did not name are absent
    from the row and keep the type's ``None`` default.
    """
    key_type = GROUP_KEY_TYPE_BY_ENTITY[entity]
    declared = {key_field.name for key_field in dataclasses.fields(key_type)}
    return key_type(**{name: value for name, value in row.items() if name in declared})
