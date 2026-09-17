"""The GraphQL surface of grouping: what may be grouped by, and what comes back.

Three things live here, one per direction of a grouped request.

**Going in**, a per-entity pair of enums names the groupable dimensions, split
by whether they take a bucket size. That split is the point: a granularity is
declared *inside* the temporal variant, and the temporal variant's enum holds
only temporal fields, so "``DAY`` on ``calendarId``" is not a runtime check that
somebody has to remember to write -- it is a document GraphQL refuses to
validate. The one part the schema cannot carry is "exactly one of the two
variants", because GraphQL has no input unions; that is
:class:`~public_api.aggregations.errors.AmbiguousGroupByError`.

**In the middle**, :func:`dimensions_from_group_by` turns those inputs into the
:class:`~public_api.aggregations.plan.DimensionSpec` tuple a plan carries,
resolving the caller's IANA timezone once for the whole request.

**Coming back**, a per-entity ``*GroupKey`` type carries every groupable
dimension as a nullable field, and :func:`build_group_key` populates only the
ones the query named. Nullable-and-mostly-empty is deliberate: one key type per
entity keeps the response shape stable across queries that group differently,
which is what lets a partner generate code against it once.

Both enums of an entity are checked against the registry at import time by
:func:`validate_dimension_enums` -- so a dimension added to the registry and not
to the schema (or the reverse) fails the process rather than quietly going
missing from one of the two.
"""

import dataclasses
import datetime
import enum
import zoneinfo
from collections.abc import Sequence
from typing import Any

import strawberry

from public_api.aggregations.errors import (
    AggregateRegistrationError,
    AmbiguousGroupByError,
    DuplicateGroupKeyFieldError,
)
from public_api.aggregations.plan import AggregatableEntity, DimensionSpec
from public_api.aggregations.registry import dimension_alias, get_registration
from public_api.aggregations.timezone import resolve_timezone
from public_api.aggregations.types import TemporalGranularity


# ---------------------------------------------------------------------------
# Per-entity group-by field enums
# ---------------------------------------------------------------------------
#
# Member names are the GraphQL enum values; member values are the registry's
# own field names, which is what a ``DimensionSpec`` carries. The two enums of
# an entity partition its registered dimensions -- every groupable field is in
# exactly one of them, asserted at import below.


@strawberry.enum(description="Scalar dimensions a calendar event may be grouped by.")
class CalendarEventScalarGroupByField(enum.Enum):
    CALENDAR_ID = "calendar_id"
    APPOINTMENT_TYPE_ID = "appointment_type_id"
    BUNDLE_CALENDAR_ID = "bundle_calendar_id"
    IS_BUNDLE_PRIMARY = "is_bundle_primary"
    IS_RECURRING_EXCEPTION = "is_recurring_exception"
    TIMEZONE = "timezone"


@strawberry.enum(description="Temporal dimensions a calendar event may be bucketed by.")
class CalendarEventTemporalGroupByField(enum.Enum):
    START_TIME = "start_time"
    END_TIME = "end_time"
    CREATED = "created"
    MODIFIED = "modified"


@strawberry.enum(description="Scalar dimensions an available time may be grouped by.")
class AvailableTimeScalarGroupByField(enum.Enum):
    CALENDAR_ID = "calendar_id"
    IS_RECURRING_EXCEPTION = "is_recurring_exception"
    TIMEZONE = "timezone"


@strawberry.enum(description="Temporal dimensions an available time may be bucketed by.")
class AvailableTimeTemporalGroupByField(enum.Enum):
    START_TIME = "start_time"
    END_TIME = "end_time"
    CREATED = "created"
    MODIFIED = "modified"


@strawberry.enum(description="Scalar dimensions a blocked time may be grouped by.")
class BlockedTimeScalarGroupByField(enum.Enum):
    CALENDAR_ID = "calendar_id"
    BUNDLE_CALENDAR_ID = "bundle_calendar_id"
    IS_RECURRING_EXCEPTION = "is_recurring_exception"
    TIMEZONE = "timezone"


@strawberry.enum(description="Temporal dimensions a blocked time may be bucketed by.")
class BlockedTimeTemporalGroupByField(enum.Enum):
    START_TIME = "start_time"
    END_TIME = "end_time"
    CREATED = "created"
    MODIFIED = "modified"


@strawberry.enum(description="Scalar dimensions an appointment type may be grouped by.")
class AppointmentTypeScalarGroupByField(enum.Enum):
    ACCEPTS_PUBLIC_SCHEDULING = "accepts_public_scheduling"


@strawberry.enum(description="Temporal dimensions an appointment type may be bucketed by.")
class AppointmentTypeTemporalGroupByField(enum.Enum):
    CREATED = "created"
    MODIFIED = "modified"


@strawberry.enum(description="Scalar dimensions a calendar may be grouped by.")
class CalendarScalarGroupByField(enum.Enum):
    CALENDAR_TYPE = "calendar_type"
    PROVIDER = "provider"
    VISIBILITY = "visibility"
    SYNC_ENABLED = "sync_enabled"
    ACCEPTS_PUBLIC_SCHEDULING = "accepts_public_scheduling"
    MANAGE_AVAILABLE_WINDOWS = "manage_available_windows"


@strawberry.enum(description="Temporal dimensions a calendar may be bucketed by.")
class CalendarTemporalGroupByField(enum.Enum):
    CREATED = "created"
    MODIFIED = "modified"


@strawberry.enum(description="Temporal dimensions a calendar pool may be bucketed by.")
class CalendarPoolTemporalGroupByField(enum.Enum):
    CREATED = "created"
    MODIFIED = "modified"


# ``CalendarPool`` registers no scalar dimension, and there is no honest one to
# invent: its columns are a name and a description, whose cardinality equals the
# row count. It is therefore the one entity with no scalar variant -- a GraphQL
# enum must have at least one value, so an empty one could not exist anyway.


# ---------------------------------------------------------------------------
# Group-by inputs
# ---------------------------------------------------------------------------


@strawberry.input(description="Bucket a calendar event's temporal dimension.")
class CalendarEventTemporalGroupBy:
    field: CalendarEventTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description="Bucket an available time's temporal dimension.")
class AvailableTimeTemporalGroupBy:
    field: AvailableTimeTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description="Bucket a blocked time's temporal dimension.")
class BlockedTimeTemporalGroupBy:
    field: BlockedTimeTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description="Bucket an appointment type's temporal dimension.")
class AppointmentTypeTemporalGroupBy:
    field: AppointmentTypeTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description="Bucket a calendar's temporal dimension.")
class CalendarTemporalGroupBy:
    field: CalendarTemporalGroupByField
    granularity: TemporalGranularity


@strawberry.input(description="Bucket a calendar pool's temporal dimension.")
class CalendarPoolTemporalGroupBy:
    field: CalendarPoolTemporalGroupByField
    granularity: TemporalGranularity


_GROUP_BY_DESCRIPTION = (
    "One group-by dimension. Name exactly one of `scalar` or `temporal`; a "
    "temporal dimension carries its own bucket size."
)


@strawberry.input(description=_GROUP_BY_DESCRIPTION)
class CalendarEventGroupByInput:
    scalar: CalendarEventScalarGroupByField | None = None
    temporal: CalendarEventTemporalGroupBy | None = None


@strawberry.input(description=_GROUP_BY_DESCRIPTION)
class AvailableTimeGroupByInput:
    scalar: AvailableTimeScalarGroupByField | None = None
    temporal: AvailableTimeTemporalGroupBy | None = None


@strawberry.input(description=_GROUP_BY_DESCRIPTION)
class BlockedTimeGroupByInput:
    scalar: BlockedTimeScalarGroupByField | None = None
    temporal: BlockedTimeTemporalGroupBy | None = None


@strawberry.input(description=_GROUP_BY_DESCRIPTION)
class AppointmentTypeGroupByInput:
    scalar: AppointmentTypeScalarGroupByField | None = None
    temporal: AppointmentTypeTemporalGroupBy | None = None


@strawberry.input(description=_GROUP_BY_DESCRIPTION)
class CalendarGroupByInput:
    scalar: CalendarScalarGroupByField | None = None
    temporal: CalendarTemporalGroupBy | None = None


@strawberry.input(
    description=(
        "One group-by dimension. A calendar pool has no scalar dimension worth "
        "grouping by, so `temporal` is the only variant."
    )
)
class CalendarPoolGroupByInput:
    temporal: CalendarPoolTemporalGroupBy | None = None


# Every group-by input this module defines, so a caller can be generic over
# entities without matching on the concrete class.
AnyGroupByInput = (
    CalendarEventGroupByInput
    | AvailableTimeGroupByInput
    | BlockedTimeGroupByInput
    | AppointmentTypeGroupByInput
    | CalendarGroupByInput
    | CalendarPoolGroupByInput
)


# ---------------------------------------------------------------------------
# Group key output types
# ---------------------------------------------------------------------------
#
# Every field nullable, populated only for the dimensions a query named. One
# key type per entity rather than one per grouping keeps the response shape
# stable across queries that group differently.


@strawberry.type(description="The dimensions a calendar-event aggregate row was grouped by.")
class CalendarEventGroupKey:
    calendar_id: int | None = None
    appointment_type_id: int | None = None
    bundle_calendar_id: int | None = None
    is_bundle_primary: bool | None = None
    is_recurring_exception: bool | None = None
    timezone: str | None = None
    start_time: datetime.datetime | None = None
    end_time: datetime.datetime | None = None
    created: datetime.datetime | None = None
    modified: datetime.datetime | None = None


@strawberry.type(description="The dimensions an available-time aggregate row was grouped by.")
class AvailableTimeGroupKey:
    calendar_id: int | None = None
    is_recurring_exception: bool | None = None
    timezone: str | None = None
    start_time: datetime.datetime | None = None
    end_time: datetime.datetime | None = None
    created: datetime.datetime | None = None
    modified: datetime.datetime | None = None


@strawberry.type(description="The dimensions a blocked-time aggregate row was grouped by.")
class BlockedTimeGroupKey:
    calendar_id: int | None = None
    bundle_calendar_id: int | None = None
    is_recurring_exception: bool | None = None
    timezone: str | None = None
    start_time: datetime.datetime | None = None
    end_time: datetime.datetime | None = None
    created: datetime.datetime | None = None
    modified: datetime.datetime | None = None


@strawberry.type(description="The dimensions an appointment-type aggregate row was grouped by.")
class AppointmentTypeGroupKey:
    accepts_public_scheduling: bool | None = None
    created: datetime.datetime | None = None
    modified: datetime.datetime | None = None


@strawberry.type(description="The dimensions a calendar aggregate row was grouped by.")
class CalendarGroupKey:
    calendar_type: str | None = None
    provider: str | None = None
    visibility: str | None = None
    sync_enabled: bool | None = None
    accepts_public_scheduling: bool | None = None
    manage_available_windows: bool | None = None
    created: datetime.datetime | None = None
    modified: datetime.datetime | None = None


@strawberry.type(description="The dimensions a calendar-pool aggregate row was grouped by.")
class CalendarPoolGroupKey:
    created: datetime.datetime | None = None
    modified: datetime.datetime | None = None


GROUP_KEY_TYPE_BY_ENTITY: dict[AggregatableEntity, type] = {
    AggregatableEntity.CALENDAR_EVENT: CalendarEventGroupKey,
    AggregatableEntity.AVAILABLE_TIME: AvailableTimeGroupKey,
    AggregatableEntity.BLOCKED_TIME: BlockedTimeGroupKey,
    AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeGroupKey,
    AggregatableEntity.CALENDAR: CalendarGroupKey,
    AggregatableEntity.CALENDAR_POOL: CalendarPoolGroupKey,
}

GROUP_BY_INPUT_TYPE_BY_ENTITY: dict[AggregatableEntity, type] = {
    AggregatableEntity.CALENDAR_EVENT: CalendarEventGroupByInput,
    AggregatableEntity.AVAILABLE_TIME: AvailableTimeGroupByInput,
    AggregatableEntity.BLOCKED_TIME: BlockedTimeGroupByInput,
    AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeGroupByInput,
    AggregatableEntity.CALENDAR: CalendarGroupByInput,
    AggregatableEntity.CALENDAR_POOL: CalendarPoolGroupByInput,
}

# Scalar enum, temporal enum. ``None`` for the scalar half means the entity has
# no scalar dimension at all.
_GROUP_BY_ENUMS_BY_ENTITY: dict[
    AggregatableEntity, tuple[type[enum.Enum] | None, type[enum.Enum]]
] = {
    AggregatableEntity.CALENDAR_EVENT: (
        CalendarEventScalarGroupByField,
        CalendarEventTemporalGroupByField,
    ),
    AggregatableEntity.AVAILABLE_TIME: (
        AvailableTimeScalarGroupByField,
        AvailableTimeTemporalGroupByField,
    ),
    AggregatableEntity.BLOCKED_TIME: (
        BlockedTimeScalarGroupByField,
        BlockedTimeTemporalGroupByField,
    ),
    AggregatableEntity.APPOINTMENT_TYPE: (
        AppointmentTypeScalarGroupByField,
        AppointmentTypeTemporalGroupByField,
    ),
    AggregatableEntity.CALENDAR: (CalendarScalarGroupByField, CalendarTemporalGroupByField),
    AggregatableEntity.CALENDAR_POOL: (None, CalendarPoolTemporalGroupByField),
}


# ---------------------------------------------------------------------------
# Input -> plan
# ---------------------------------------------------------------------------


def dimensions_from_group_by(
    entity: AggregatableEntity,
    group_by: "Sequence[AnyGroupByInput]",
    timezone_name: str,
) -> tuple[DimensionSpec, ...]:
    """Turn a field's ``groupBy`` argument into the plan's dimension tuple.

    ``timezone_name`` is the field's IANA ``timezone`` argument, resolved once
    here and handed to every temporal dimension, so one result set is bucketed
    on one wall clock. It is validated even when nothing is bucketed: it is a
    non-null argument, and accepting a name in one query that another query
    would refuse is the kind of inconsistency partners write bug reports about.
    """
    tzinfo = resolve_timezone(timezone_name)

    dimensions: list[DimensionSpec] = []
    claimed: set[str] = set()
    for entry in group_by:
        dimension = _dimension_from_entry(entry, tzinfo)
        if dimension.field_path in claimed:
            raise DuplicateGroupKeyFieldError(dimension.field_path)
        claimed.add(dimension.field_path)
        dimensions.append(dimension)

    # Not this module's call to make: a plan with no dimensions is refused by
    # ``AggregateQueryPlan`` itself, with the reason attached.
    return tuple(dimensions)


def _dimension_from_entry(entry: "AnyGroupByInput", tzinfo: zoneinfo.ZoneInfo) -> DimensionSpec:
    """One ``groupBy`` entry as a :class:`DimensionSpec`.

    ``getattr`` rather than attribute access because ``CalendarPoolGroupByInput``
    has no ``scalar`` field at all -- there is no scalar dimension to name.
    """
    scalar = getattr(entry, "scalar", None)
    temporal = getattr(entry, "temporal", None)

    if scalar is not None and temporal is not None:
        raise AmbiguousGroupByError

    if temporal is not None:
        temporal_name = temporal.field.value
        return DimensionSpec(
            alias=dimension_alias(temporal_name, temporal.granularity),
            field_path=temporal_name,
            granularity=temporal.granularity,
            tzinfo=tzinfo,
        )

    if scalar is not None:
        scalar_name = scalar.value
        return DimensionSpec(alias=dimension_alias(scalar_name), field_path=scalar_name)

    raise AmbiguousGroupByError


# ---------------------------------------------------------------------------
# Row -> group key
# ---------------------------------------------------------------------------


def build_group_key(
    entity: AggregatableEntity,
    dimensions: tuple[DimensionSpec, ...],
    row: dict[str, Any],
) -> Any:
    """Populate ``entity``'s group-key type from one aggregated row.

    Only the dimensions the query named are set; the rest stay ``None``. This
    is a field-by-field copy out of the row dict, not a computation -- the
    database already decided every value here.
    """
    key_class = GROUP_KEY_TYPE_BY_ENTITY[entity]
    values = {
        dimension.field_path: row.get(dimension.alias)
        for dimension in dimensions
        if dimension.alias in row
    }
    return key_class(**values)


# ---------------------------------------------------------------------------
# Import-time conformance
# ---------------------------------------------------------------------------


def validate_dimension_enums() -> None:
    """Assert every entity's two enums partition its registered dimensions.

    Run at import. A dimension added to the registry but not to an enum would
    otherwise be silently unreachable from the schema, and one removed from the
    registry but left in an enum would be a value a caller can send and no
    resolver can honour -- both of which look like nothing at all until a
    partner asks why a field they can see returns an error.
    """
    for entity, (scalar_enum, temporal_enum) in _GROUP_BY_ENUMS_BY_ENTITY.items():
        registration = get_registration(entity)
        registered_scalar = {spec.name for spec in registration.groupable if not spec.temporal}
        registered_temporal = {spec.name for spec in registration.groupable if spec.temporal}

        declared_scalar = {member.value for member in scalar_enum} if scalar_enum else set()
        declared_temporal = {member.value for member in temporal_enum}

        if declared_scalar != registered_scalar:
            raise AggregateRegistrationError(
                f"{entity.value}: scalar group-by enum {sorted(declared_scalar)} does not "
                f"match the registry's scalar dimensions {sorted(registered_scalar)}"
            )
        if declared_temporal != registered_temporal:
            raise AggregateRegistrationError(
                f"{entity.value}: temporal group-by enum {sorted(declared_temporal)} does "
                f"not match the registry's temporal dimensions {sorted(registered_temporal)}"
            )

        key_fields = {field.name for field in dataclasses.fields(GROUP_KEY_TYPE_BY_ENTITY[entity])}
        missing = (registered_scalar | registered_temporal) - key_fields
        if missing:
            raise AggregateRegistrationError(
                f"{entity.value}: group key type has no field for {sorted(missing)}"
            )


validate_dimension_enums()
