"""What each entity may be grouped by, and what may be aggregated over it.

One place decides that ``title`` gets :class:`StringAggregate` and never
:class:`NumericAggregate`. Six entity surfaces read that decision instead of
each making it, which is the whole reason this module is separate from the
per-entity GraphQL types that will consume it.

Three things are registered per entity:

* **dimensions** -- the columns a caller may ``GROUP BY``. A dimension is a
  key, not a value, so the set is deliberately small: foreign keys, low
  cardinality choice columns, booleans, and the temporal columns.
* **metrics** -- the columns a caller may aggregate. Each carries a
  :class:`FieldKind`, and the kind decides both the GraphQL type it is
  exposed as and the operations it will accept.
* **relation counts** -- counts over a related table. These are *not*
  ordinary metrics: several ``Count()`` calls over different relations in
  one ``annotate()`` fan the joins out and multiply each other's answers, so
  the executor builds them as correlated subqueries instead. See
  ``public_api/queries.py`` (``childOrganizations``), which hit and fixed the
  same bug.

Nothing here is tenant-aware, by design. Scoping comes from the model
managers and from the base queryset the executor is handed -- a registry
that also filtered would be a second place for tenancy to be wrong.
"""

import enum
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from django.db.models import DurationField, ExpressionWrapper, F, Model
from django.db.models.expressions import Combinable
from django.db.models.functions import Extract

from calendar_integration.models import (
    AppointmentType,
    AppointmentTypeSlot,
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarPool,
    CalendarPoolMembership,
    EventAttendance,
    EventExternalAttendance,
    ResourceAllocation,
)
from public_api.aggregations.errors import (
    UnknownDimensionError,
    UnknownEntityError,
    UnknownMetricFieldError,
    UnsupportedOperationError,
)
from public_api.aggregations.plan import AggregatableEntity, AggregateOp
from public_api.aggregations.types import (
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
)
from public_api.constants import PublicAPIResources


class FieldKind(enum.Enum):
    """The four kinds of aggregatable column this engine recognises."""

    NUMERIC = "numeric"
    STRING = "string"
    DATETIME = "datetime"
    BOOLEAN = "boolean"


#: Field kind to the GraphQL type that exposes it. Exactly one entry per
#: kind, which is what makes "every aggregatable field maps to exactly one
#: aggregate type" a property of the table rather than a convention.
AGGREGATE_TYPE_BY_KIND: Mapping[FieldKind, type] = MappingProxyType(
    {
        FieldKind.NUMERIC: NumericAggregate,
        FieldKind.STRING: StringAggregate,
        FieldKind.DATETIME: DateTimeAggregate,
        FieldKind.BOOLEAN: BooleanAggregate,
    }
)

#: Which operations each kind accepts. ``CONCAT`` is absent from every kind
#: but ``STRING``; ``SUM`` / ``AVG`` are absent from everything but
#: ``NUMERIC``. The GraphQL types above enforce the same thing for schema
#: callers -- this table is the answer for code that builds a plan directly.
OPS_BY_KIND: Mapping[FieldKind, frozenset[AggregateOp]] = MappingProxyType(
    {
        FieldKind.NUMERIC: frozenset(
            {AggregateOp.SUM, AggregateOp.AVG, AggregateOp.MIN, AggregateOp.MAX}
        ),
        FieldKind.STRING: frozenset({AggregateOp.CONCAT, AggregateOp.MIN, AggregateOp.MAX}),
        FieldKind.DATETIME: frozenset({AggregateOp.MIN, AggregateOp.MAX}),
        FieldKind.BOOLEAN: frozenset({AggregateOp.TRUE_COUNT, AggregateOp.FALSE_COUNT}),
    }
)


def _duration_minutes(start: str, end: str) -> Combinable:
    """Minutes between two datetime columns, as a database expression.

    ``RecurringMixin`` exposes duration as a Python property over
    ``end_time - start_time``, which SQL cannot see. Postgres subtracts the
    two generated columns into an ``interval`` and ``EXTRACT(EPOCH FROM ...)``
    turns that into seconds; the division makes it minutes.
    """
    delta = ExpressionWrapper(F(end) - F(start), output_field=DurationField())
    return Extract(delta, "epoch") / 60.0


def _duration_field_minutes(path: str) -> Combinable:
    """Minutes held by a ``DurationField`` column."""
    return Extract(F(path), "epoch") / 60.0


@dataclass(frozen=True)
class GroupableDimension:
    """One column a caller may group by.

    ``temporal`` marks the columns that accept a granularity. It is carried
    here rather than inferred from the model field so a plain ``DateTimeField``
    that should not be bucketed (an audit timestamp, say) can be registered
    as a key without becoming a time series axis.
    """

    field_path: str
    temporal: bool = False


@dataclass(frozen=True)
class AggregatableField:
    """One column a caller may aggregate.

    ``expression_factory`` is set for the handful of metrics that are not a
    bare column -- ``duration_minutes`` is computed from two of them. It is a
    factory rather than a shared expression instance so no two querysets ever
    hold the same node.
    """

    kind: FieldKind
    field_path: str
    expression_factory: Callable[[], Combinable] | None = None

    @property
    def aggregate_type(self) -> type:
        """The GraphQL type this field is exposed as."""
        return AGGREGATE_TYPE_BY_KIND[self.kind]

    @property
    def supported_ops(self) -> frozenset[AggregateOp]:
        """The operations this field accepts."""
        return OPS_BY_KIND[self.kind]


@dataclass(frozen=True)
class RelationCount:
    """A count of related rows, executed as a correlated subquery.

    ``relation_column`` is the concrete foreign key column on the *related*
    model that points back at the entity being grouped -- the ``<name>_fk_id``
    an ``OrganizationSafeForeignKey`` creates. The executor correlates it
    against the grouped model's primary key.
    """

    model: type[Model]
    relation_column: str


@dataclass(frozen=True)
class EntityRegistration:
    """Everything the engine knows about one aggregatable entity."""

    entity: AggregatableEntity
    model: type[Model]
    #: The resource a caller must already hold to page these rows one at a
    #: time. An aggregate requires the same one and never a dedicated
    #: analytics resource, so it discloses nothing the caller could not
    #: already read.
    resource: str
    dimensions: Mapping[str, GroupableDimension]
    metrics: Mapping[str, AggregatableField]
    relation_counts: Mapping[str, RelationCount] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimensions", MappingProxyType(dict(self.dimensions)))
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        object.__setattr__(self, "relation_counts", MappingProxyType(dict(self.relation_counts)))

    def dimension(self, field_path: str) -> GroupableDimension:
        """Look up a groupable dimension, raising when it is not registered."""
        try:
            return self.dimensions[field_path]
        except KeyError:
            raise UnknownDimensionError(self.entity, field_path) from None

    def metric(self, field_path: str) -> AggregatableField:
        """Look up an aggregatable field, raising when it is not registered.

        Raising is the point: a lookup that returned ``None`` would let a
        typo'd field path produce a result set that is simply missing a
        column, which nothing downstream would notice.
        """
        try:
            return self.metrics[field_path]
        except KeyError:
            raise UnknownMetricFieldError(self.entity, field_path) from None

    def checked_metric(self, field_path: str, op: AggregateOp) -> AggregatableField:
        """Look up an aggregatable field and confirm it accepts ``op``."""
        aggregatable = self.metric(field_path)
        if op not in aggregatable.supported_ops:
            raise UnsupportedOperationError(op, field_path, aggregatable.kind)
        return aggregatable


# ---------------------------------------------------------------------------
# Shared field sets
# ---------------------------------------------------------------------------
#
# ``CalendarEvent``, ``BlockedTime`` and ``AvailableTime`` all descend from
# ``RecurringMixin``, so they share its columns. Spelling them once keeps the
# three from drifting.

_RECURRING_DIMENSIONS: Mapping[str, GroupableDimension] = MappingProxyType(
    {
        # Grouping by the row's own key is what turns a relation count into a
        # per-row count rather than a per-group total. Every entity offers it.
        "id": GroupableDimension("id"),
        "calendar_fk_id": GroupableDimension("calendar_fk_id"),
        "timezone": GroupableDimension("timezone"),
        "is_recurring_exception": GroupableDimension("is_recurring_exception"),
        "start_time": GroupableDimension("start_time", temporal=True),
        "end_time": GroupableDimension("end_time", temporal=True),
        "created": GroupableDimension("created", temporal=True),
    }
)

_RECURRING_METRICS: Mapping[str, AggregatableField] = MappingProxyType(
    {
        "duration_minutes": AggregatableField(
            FieldKind.NUMERIC,
            "duration_minutes",
            expression_factory=lambda: _duration_minutes("start_time", "end_time"),
        ),
        "start_time": AggregatableField(FieldKind.DATETIME, "start_time"),
        "end_time": AggregatableField(FieldKind.DATETIME, "end_time"),
        "created": AggregatableField(FieldKind.DATETIME, "created"),
        "is_recurring_exception": AggregatableField(FieldKind.BOOLEAN, "is_recurring_exception"),
    }
)


def _dimensions_with(
    base: Mapping[str, GroupableDimension], **extra: GroupableDimension
) -> Mapping[str, GroupableDimension]:
    """The shared dimensions plus per-entity ones, as a new immutable mapping."""
    return MappingProxyType({**base, **extra})


def _metrics_with(
    base: Mapping[str, AggregatableField], **extra: AggregatableField
) -> Mapping[str, AggregatableField]:
    """The shared metrics plus per-entity ones, as a new immutable mapping."""
    return MappingProxyType({**base, **extra})


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
#
# Dimensions name the CONCRETE ``<name>_fk_id`` column rather than traversing
# the organization-safe ``<name>`` relation. That is deliberate and is not the
# ``_fk`` shortcut the project forbids: grouping reads a column off the row
# being grouped, so there is no ``ON`` clause for the organization to fall out
# of. Going through ``<name>`` would add a join to the ``GROUP BY`` for a
# value already present on the row. Tenant scoping is in the base queryset's
# ``WHERE``, where it belongs.

_REGISTRY: Mapping[AggregatableEntity, EntityRegistration] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: EntityRegistration(
            entity=AggregatableEntity.CALENDAR_EVENT,
            model=CalendarEvent,
            resource=PublicAPIResources.CALENDAR_EVENT,
            dimensions=_dimensions_with(
                _RECURRING_DIMENSIONS,
                appointment_type_fk_id=GroupableDimension("appointment_type_fk_id"),
                bundle_calendar_fk_id=GroupableDimension("bundle_calendar_fk_id"),
                is_bundle_primary=GroupableDimension("is_bundle_primary"),
            ),
            metrics=_metrics_with(
                _RECURRING_METRICS,
                title=AggregatableField(FieldKind.STRING, "title"),
                description=AggregatableField(FieldKind.STRING, "description"),
                is_bundle_primary=AggregatableField(FieldKind.BOOLEAN, "is_bundle_primary"),
            ),
            relation_counts={
                "attendance_count": RelationCount(EventAttendance, "event_fk_id"),
                "external_attendance_count": RelationCount(EventExternalAttendance, "event_fk_id"),
                "resource_allocation_count": RelationCount(ResourceAllocation, "event_fk_id"),
            },
        ),
        AggregatableEntity.AVAILABLE_TIME: EntityRegistration(
            entity=AggregatableEntity.AVAILABLE_TIME,
            model=AvailableTime,
            resource=PublicAPIResources.AVAILABLE_TIME,
            dimensions=_RECURRING_DIMENSIONS,
            metrics=_RECURRING_METRICS,
        ),
        AggregatableEntity.BLOCKED_TIME: EntityRegistration(
            entity=AggregatableEntity.BLOCKED_TIME,
            model=BlockedTime,
            resource=PublicAPIResources.BLOCKED_TIME,
            dimensions=_dimensions_with(
                _RECURRING_DIMENSIONS,
                bundle_calendar_fk_id=GroupableDimension("bundle_calendar_fk_id"),
            ),
            metrics=_metrics_with(
                _RECURRING_METRICS,
                reason=AggregatableField(FieldKind.STRING, "reason"),
            ),
        ),
        AggregatableEntity.APPOINTMENT_TYPE: EntityRegistration(
            entity=AggregatableEntity.APPOINTMENT_TYPE,
            model=AppointmentType,
            resource=PublicAPIResources.APPOINTMENT_TYPE,
            dimensions={
                "id": GroupableDimension("id"),
                "accepts_public_scheduling": GroupableDimension("accepts_public_scheduling"),
                "created": GroupableDimension("created", temporal=True),
            },
            metrics={
                "name": AggregatableField(FieldKind.STRING, "name"),
                "description": AggregatableField(FieldKind.STRING, "description"),
                "created": AggregatableField(FieldKind.DATETIME, "created"),
                "accepts_public_scheduling": AggregatableField(
                    FieldKind.BOOLEAN, "accepts_public_scheduling"
                ),
                "duration_minutes": AggregatableField(
                    FieldKind.NUMERIC,
                    "duration_minutes",
                    expression_factory=lambda: _duration_field_minutes("duration"),
                ),
            },
            relation_counts={
                "event_count": RelationCount(CalendarEvent, "appointment_type_fk_id"),
                "slot_count": RelationCount(AppointmentTypeSlot, "appointment_type_fk_id"),
            },
        ),
        AggregatableEntity.CALENDAR: EntityRegistration(
            entity=AggregatableEntity.CALENDAR,
            model=Calendar,
            resource=PublicAPIResources.CALENDAR,
            dimensions={
                "id": GroupableDimension("id"),
                "provider": GroupableDimension("provider"),
                "calendar_type": GroupableDimension("calendar_type"),
                "visibility": GroupableDimension("visibility"),
                "manage_available_windows": GroupableDimension("manage_available_windows"),
                "accepts_public_scheduling": GroupableDimension("accepts_public_scheduling"),
                "sync_enabled": GroupableDimension("sync_enabled"),
                "created": GroupableDimension("created", temporal=True),
            },
            metrics={
                "name": AggregatableField(FieldKind.STRING, "name"),
                "description": AggregatableField(FieldKind.STRING, "description"),
                "capacity": AggregatableField(FieldKind.NUMERIC, "capacity"),
                "created": AggregatableField(FieldKind.DATETIME, "created"),
                "manage_available_windows": AggregatableField(
                    FieldKind.BOOLEAN, "manage_available_windows"
                ),
                "accepts_public_scheduling": AggregatableField(
                    FieldKind.BOOLEAN, "accepts_public_scheduling"
                ),
                "sync_enabled": AggregatableField(FieldKind.BOOLEAN, "sync_enabled"),
            },
            relation_counts={
                "event_count": RelationCount(CalendarEvent, "calendar_fk_id"),
                "blocked_time_count": RelationCount(BlockedTime, "calendar_fk_id"),
                "available_time_count": RelationCount(AvailableTime, "calendar_fk_id"),
            },
        ),
        AggregatableEntity.CALENDAR_POOL: EntityRegistration(
            entity=AggregatableEntity.CALENDAR_POOL,
            model=CalendarPool,
            resource=PublicAPIResources.CALENDAR_POOL,
            dimensions={
                "id": GroupableDimension("id"),
                "created": GroupableDimension("created", temporal=True),
            },
            metrics={
                "name": AggregatableField(FieldKind.STRING, "name"),
                "description": AggregatableField(FieldKind.STRING, "description"),
                "created": AggregatableField(FieldKind.DATETIME, "created"),
            },
            relation_counts={
                "calendar_count": RelationCount(CalendarPoolMembership, "pool_fk_id"),
            },
        ),
    }
)


def get_registration(entity: AggregatableEntity) -> EntityRegistration:
    """The registry entry for ``entity``, raising when there is none."""
    try:
        return _REGISTRY[entity]
    except KeyError:
        raise UnknownEntityError(entity) from None


def registered_entities() -> tuple[AggregatableEntity, ...]:
    """Every entity the engine can group, in registration order."""
    return tuple(_REGISTRY)


def aggregate_type_for(kind: FieldKind) -> type:
    """The GraphQL type a field of ``kind`` is exposed as."""
    return AGGREGATE_TYPE_BY_KIND[kind]


def supported_ops(kind: FieldKind) -> frozenset[AggregateOp]:
    """The operations a field of ``kind`` accepts."""
    return OPS_BY_KIND[kind]
