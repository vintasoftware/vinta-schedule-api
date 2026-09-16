"""The one place that decides what each entity may be grouped by and aggregated over.

Two mappings live here and nothing else is allowed to hold an opinion about
either:

1. **field kind -> aggregate type and operations.** ``title`` is a string, so it
   gets ``StringAggregate`` and the operations ``concat`` / ``min`` / ``max``,
   and it can never get ``sum``. Six entity surfaces reading this one table is
   what stops the six from drifting into subtly different answers to the same
   question.
2. **entity -> registration.** Which model backs it, which resource scope it
   requires, which field its mandatory date range is measured on, and the
   closed sets of groupable fields, aggregatable fields and countable
   relations.

Field selection is conservative by construction: a column that is not listed
here cannot be reached through an aggregate, whatever the caller asks for.
Nothing here widens what a token can already read -- an aggregate field
requires the same resource as the entity's list field, so it discloses no
column the caller could not already page through one row at a time.
"""

import enum
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from django.db.models import F, FloatField, Model
from django.db.models.expressions import Combinable, ExpressionWrapper, Value
from django.db.models.functions import Extract

from calendar_integration.models import (
    AppointmentType,
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarPool,
)
from common.managers import OrganizationScopedManager
from public_api.aggregations import errors
from public_api.aggregations.plan import AggregatableEntity, AggregateOp
from public_api.aggregations.types import (
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
)
from public_api.constants import PublicAPIResources


class AggregateKind(enum.StrEnum):
    """The kind of value a field holds, which decides its aggregate type."""

    NUMERIC = "numeric"
    STRING = "string"
    DATETIME = "datetime"
    BOOLEAN = "boolean"


#: Field kind -> the GraphQL type its aggregates are exposed through.
AGGREGATE_TYPE_BY_KIND: Mapping[AggregateKind, type] = MappingProxyType(
    {
        AggregateKind.NUMERIC: NumericAggregate,
        AggregateKind.STRING: StringAggregate,
        AggregateKind.DATETIME: DateTimeAggregate,
        AggregateKind.BOOLEAN: BooleanAggregate,
    }
)

#: Field kind -> the operations that type exposes. Kept in step with
#: ``AGGREGATE_TYPE_BY_KIND`` by ``test_registry``, which asserts the two agree.
OPS_BY_KIND: Mapping[AggregateKind, tuple[AggregateOp, ...]] = MappingProxyType(
    {
        AggregateKind.NUMERIC: (
            AggregateOp.SUM,
            AggregateOp.AVG,
            AggregateOp.MIN,
            AggregateOp.MAX,
        ),
        AggregateKind.STRING: (AggregateOp.CONCAT, AggregateOp.MIN, AggregateOp.MAX),
        AggregateKind.DATETIME: (AggregateOp.MIN, AggregateOp.MAX),
        AggregateKind.BOOLEAN: (AggregateOp.TRUE_COUNT, AggregateOp.FALSE_COUNT),
    }
)


def _minutes_between(start_path: str, end_path: str) -> Callable[[], Combinable]:
    """Build the derived "duration in minutes" expression for two datetime columns.

    ``CalendarEvent`` and friends store a start and an end, never a duration, so
    "average meeting length" is an expression rather than a column. Computed in
    SQL (``EXTRACT(EPOCH FROM end - start) / 60``) like every other aggregate
    here -- nothing about it is reshaped in Python.
    """

    def build() -> Combinable:
        return ExpressionWrapper(
            Extract(F(end_path) - F(start_path), "epoch") / Value(60.0),
            output_field=FloatField(),
        )

    return build


def _minutes_of(duration_path: str) -> Callable[[], Combinable]:
    """Build the derived "duration in minutes" expression for an interval column."""

    def build() -> Combinable:
        return ExpressionWrapper(
            Extract(F(duration_path), "epoch") / Value(60.0),
            output_field=FloatField(),
        )

    return build


@dataclass(frozen=True)
class AggregatableField:
    """One field an entity may be aggregated over.

    ``field_path`` is the ORM path. It traverses the organization-safe relation
    (``calendar__id``) rather than the concrete ``calendar_fk_id`` column, so
    the join keeps its organization-matched ``ON`` clause; Django collapses that
    particular traversal back to the local column anyway, so it costs no join.

    ``expression`` is set only for derived values -- there is no
    ``duration_minutes`` column anywhere.
    """

    name: str
    field_path: str
    kind: AggregateKind
    expression: Callable[[], Combinable] | None = None
    description: str = ""

    def build_source(self) -> Combinable:
        """Return the expression this field's aggregates are computed over."""
        if self.expression is not None:
            return self.expression()
        return F(self.field_path)

    @property
    def aggregate_type(self) -> type:
        """The GraphQL type this field's aggregates are exposed through."""
        return AGGREGATE_TYPE_BY_KIND[self.kind]

    @property
    def supported_ops(self) -> tuple[AggregateOp, ...]:
        """The operations this field exposes, decided solely by its kind."""
        return OPS_BY_KIND[self.kind]


@dataclass(frozen=True)
class GroupableField:
    """One dimension an entity may be grouped by.

    ``is_temporal`` decides whether a ``granularity`` may be attached. A
    granularity on a non-temporal dimension is refused here as a backstop; on
    the GraphQL surface it is unrepresentable, because the temporal and
    non-temporal group-by inputs are separate types.
    """

    name: str
    field_path: str
    is_temporal: bool = False
    description: str = ""


@dataclass(frozen=True)
class CountableRelation:
    """One related collection an entity may be counted over.

    Counted through a correlated subquery rather than a join, so that two of
    them in a single query cannot multiply each other's rows -- the fan-out bug
    ``public_api.queries.child_organizations`` already had to solve.
    """

    name: str
    relation_path: str
    description: str = ""


@dataclass(frozen=True)
class EntityRegistration:
    """Everything the engine knows about one aggregatable entity family."""

    entity: AggregatableEntity
    model: type[Model]
    resource: PublicAPIResources
    date_range_field: str
    groupable: Mapping[str, GroupableField]
    aggregatable: Mapping[str, AggregatableField]
    countable_relations: Mapping[str, CountableRelation] = MappingProxyType({})

    def get_groupable(self, name: str) -> GroupableField:
        """Return the groupable dimension `name`, or raise."""
        try:
            return self.groupable[name]
        except KeyError:
            raise errors.UnknownFieldError(
                f"{self.entity.value!r} has no groupable field {name!r}"
            ) from None

    def get_aggregatable(self, name: str) -> AggregatableField:
        """Return the aggregatable field `name`, or raise."""
        try:
            return self.aggregatable[name]
        except KeyError:
            raise errors.UnknownFieldError(
                f"{self.entity.value!r} has no aggregatable field {name!r}"
            ) from None

    def get_countable_relation(self, name: str) -> CountableRelation:
        """Return the countable relation `name`, or raise."""
        try:
            return self.countable_relations[name]
        except KeyError:
            raise errors.UnknownFieldError(
                f"{self.entity.value!r} has no countable relation {name!r}"
            ) from None


def _registration(
    entity: AggregatableEntity,
    model: type[Model],
    resource: PublicAPIResources,
    date_range_field: str,
    groupable: tuple[GroupableField, ...],
    aggregatable: tuple[AggregatableField, ...],
    countable_relations: tuple[CountableRelation, ...] = (),
) -> EntityRegistration:
    """Assemble one registration, keying the field tuples by name."""
    return EntityRegistration(
        entity=entity,
        model=model,
        resource=resource,
        date_range_field=date_range_field,
        groupable=MappingProxyType({item.name: item for item in groupable}),
        aggregatable=MappingProxyType({item.name: item for item in aggregatable}),
        countable_relations=MappingProxyType({item.name: item for item in countable_relations}),
    )


# ---------------------------------------------------------------------------
# Dimensions and metrics shared by the three ``RecurringMixin`` entities.
#
# ``start_time`` / ``end_time`` are the generated, timezone-aware columns. The
# editable ``start_time_tz_unaware`` / ``end_time_tz_unaware`` are deliberately
# absent: comparing those across rows in different timezones produces wrong
# results, which is exactly what a GROUP BY over them would do quietly.
# ---------------------------------------------------------------------------

_TIMED_GROUPABLE: tuple[GroupableField, ...] = (
    GroupableField("calendar_id", "calendar__id", description="Owning calendar."),
    GroupableField("start_time", "start_time", is_temporal=True, description="Start instant."),
    GroupableField("end_time", "end_time", is_temporal=True, description="End instant."),
    GroupableField("created", "created", is_temporal=True, description="Row creation instant."),
)

_TIMED_AGGREGATABLE: tuple[AggregatableField, ...] = (
    AggregatableField(
        "duration_minutes",
        "",
        AggregateKind.NUMERIC,
        expression=_minutes_between("start_time", "end_time"),
        description="Length in minutes, computed in SQL from the start and end instants.",
    ),
    AggregatableField("start_time", "start_time", AggregateKind.DATETIME),
    AggregatableField("end_time", "end_time", AggregateKind.DATETIME),
    AggregatableField("created", "created", AggregateKind.DATETIME),
)


_REGISTRY: Mapping[AggregatableEntity, EntityRegistration] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: _registration(
            entity=AggregatableEntity.CALENDAR_EVENT,
            model=CalendarEvent,
            resource=PublicAPIResources.CALENDAR_EVENT,
            date_range_field="start_time",
            groupable=(
                *_TIMED_GROUPABLE,
                GroupableField(
                    "appointment_type_id",
                    "appointment_type__id",
                    description="Appointment type the event was booked through.",
                ),
                GroupableField(
                    "bundle_calendar_id",
                    "bundle_calendar__id",
                    description="Bundle calendar the event was created through.",
                ),
                GroupableField("is_bundle_primary", "is_bundle_primary"),
            ),
            aggregatable=(
                *_TIMED_AGGREGATABLE,
                AggregatableField("title", "title", AggregateKind.STRING),
                AggregatableField("description", "description", AggregateKind.STRING),
                AggregatableField("is_bundle_primary", "is_bundle_primary", AggregateKind.BOOLEAN),
            ),
            countable_relations=(
                CountableRelation("attendances", "attendances", "Internal attendee rows."),
                CountableRelation(
                    "external_attendances", "external_attendances", "External attendee rows."
                ),
                CountableRelation(
                    "resource_allocations", "resource_allocations", "Allocated resource calendars."
                ),
            ),
        ),
        AggregatableEntity.AVAILABLE_TIME: _registration(
            entity=AggregatableEntity.AVAILABLE_TIME,
            model=AvailableTime,
            resource=PublicAPIResources.AVAILABLE_TIME,
            date_range_field="start_time",
            groupable=_TIMED_GROUPABLE,
            aggregatable=_TIMED_AGGREGATABLE,
        ),
        AggregatableEntity.BLOCKED_TIME: _registration(
            entity=AggregatableEntity.BLOCKED_TIME,
            model=BlockedTime,
            resource=PublicAPIResources.BLOCKED_TIME,
            date_range_field="start_time",
            groupable=_TIMED_GROUPABLE,
            aggregatable=(
                *_TIMED_AGGREGATABLE,
                AggregatableField("reason", "reason", AggregateKind.STRING),
            ),
        ),
        AggregatableEntity.APPOINTMENT_TYPE: _registration(
            entity=AggregatableEntity.APPOINTMENT_TYPE,
            model=AppointmentType,
            resource=PublicAPIResources.APPOINTMENT_TYPE,
            # No start/end instants on this model: the bounded range every
            # aggregate must carry is measured on the row's creation instant.
            date_range_field="created",
            groupable=(
                GroupableField("accepts_public_scheduling", "accepts_public_scheduling"),
                GroupableField("created", "created", is_temporal=True),
            ),
            aggregatable=(
                AggregatableField("name", "name", AggregateKind.STRING),
                AggregatableField("description", "description", AggregateKind.STRING),
                AggregatableField(
                    "duration_minutes",
                    "",
                    AggregateKind.NUMERIC,
                    expression=_minutes_of("duration"),
                    description="Pinned booking length in minutes, computed in SQL.",
                ),
                AggregatableField(
                    "accepts_public_scheduling",
                    "accepts_public_scheduling",
                    AggregateKind.BOOLEAN,
                ),
                AggregatableField("created", "created", AggregateKind.DATETIME),
            ),
            countable_relations=(
                CountableRelation("slots", "slots", "Slots defined on the appointment type."),
                CountableRelation("events", "events", "Events booked through it."),
            ),
        ),
        AggregatableEntity.CALENDAR: _registration(
            entity=AggregatableEntity.CALENDAR,
            model=Calendar,
            resource=PublicAPIResources.CALENDAR,
            date_range_field="created",
            groupable=(
                GroupableField("provider", "provider"),
                GroupableField("calendar_type", "calendar_type"),
                GroupableField("visibility", "visibility"),
                GroupableField("sync_enabled", "sync_enabled"),
                GroupableField("manage_available_windows", "manage_available_windows"),
                GroupableField("accepts_public_scheduling", "accepts_public_scheduling"),
                GroupableField("created", "created", is_temporal=True),
            ),
            aggregatable=(
                AggregatableField("name", "name", AggregateKind.STRING),
                AggregatableField("description", "description", AggregateKind.STRING),
                AggregatableField(
                    "capacity",
                    "capacity",
                    AggregateKind.NUMERIC,
                    description="Resource-calendar capacity; null on other calendar types.",
                ),
                AggregatableField("sync_enabled", "sync_enabled", AggregateKind.BOOLEAN),
                AggregatableField(
                    "accepts_public_scheduling", "accepts_public_scheduling", AggregateKind.BOOLEAN
                ),
                AggregatableField(
                    "manage_available_windows",
                    "manage_available_windows",
                    AggregateKind.BOOLEAN,
                ),
                AggregatableField("created", "created", AggregateKind.DATETIME),
            ),
            countable_relations=(
                CountableRelation("events", "events", "Events on the calendar."),
                CountableRelation("blocked_times", "blocked_times", "Blocked windows."),
                CountableRelation("available_times", "available_times", "Available windows."),
            ),
        ),
        AggregatableEntity.CALENDAR_POOL: _registration(
            entity=AggregatableEntity.CALENDAR_POOL,
            model=CalendarPool,
            resource=PublicAPIResources.CALENDAR_POOL,
            date_range_field="created",
            groupable=(GroupableField("created", "created", is_temporal=True),),
            aggregatable=(
                AggregatableField("name", "name", AggregateKind.STRING),
                AggregatableField("description", "description", AggregateKind.STRING),
                AggregatableField("created", "created", AggregateKind.DATETIME),
            ),
            countable_relations=(
                CountableRelation("memberships", "memberships", "Calendars on the roster."),
            ),
        ),
    }
)


def get_registration(entity: AggregatableEntity) -> EntityRegistration:
    """Return the registration for `entity`, or raise ``UnknownEntityError``."""
    try:
        return _REGISTRY[entity]
    except KeyError:
        raise errors.UnknownEntityError(f"No aggregate registration for {entity!r}") from None


def registered_entities() -> tuple[AggregatableEntity, ...]:
    """Every entity that has a registration, in declaration order."""
    return tuple(_REGISTRY)


def aggregate_type_for_field(entity: AggregatableEntity, name: str) -> type:
    """Return the single GraphQL aggregate type field `name` is exposed through."""
    return get_registration(entity).get_aggregatable(name).aggregate_type


def assert_op_supported(field: AggregatableField, op: AggregateOp) -> None:
    """Raise unless `op` is one of the operations `field`'s kind exposes."""
    if op not in field.supported_ops:
        raise errors.UnsupportedOperationError(
            f"{op.value!r} is not available on {field.name!r} "
            f"({field.kind.value}); available: {[o.value for o in field.supported_ops]}"
        )


def assert_granularity_allowed(field: GroupableField, has_granularity: bool) -> None:
    """Raise when a granularity is attached to a non-temporal dimension."""
    if has_granularity and not field.is_temporal:
        raise errors.UnsupportedOperationError(
            f"{field.name!r} is not a temporal dimension and takes no granularity"
        )


def assert_organization_scoped(model: type[Model]) -> None:
    """Raise unless `model`'s default manager scopes reads by organization.

    The aggregation engine never reaches for ``original_manager`` or
    ``unscoped()``, and this turns that convention into a check: a ``GROUP BY``
    over an unscoped queryset would aggregate across tenants, which is the one
    bug in this feature that would not look like a bug in its output.
    """
    manager = model._default_manager
    if not isinstance(manager, OrganizationScopedManager):
        raise errors.AggregationConfigurationError(
            f"{model.__name__} does not scope reads by organization "
            f"(default manager is {type(manager).__name__})"
        )
