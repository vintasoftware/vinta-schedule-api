"""The one place that decides what each entity can group by and aggregate.

Two mappings live here and nowhere else:

* **field kind to aggregate type** — a ``STRING`` field gets ``StringAggregate``
  and can never get ``NumericAggregate``, so ``title { sum }`` is a schema
  error rather than a resolver error.
* **entity to its groupable and aggregatable fields** — six entities, one table
  each. A field that is not registered is not reachable, and asking for one
  raises instead of quietly producing no metric.

Keeping both here is what stops six entity wirings from growing six slightly
different ideas of what a string aggregate is.

Nothing in this module touches ``original_manager`` or ``unscoped()``: the
querysets an aggregate runs over come from the models' own
``OrganizationScopedManager``, and the relation-count subqueries below do too.
"""

import enum
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from django.db import models
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
    BUCKETING_NEEDS_TIMEZONE_MESSAGE,
    InvalidAggregatePlanError,
    UnknownAggregateEntityError,
    UnknownAggregateFieldError,
    UnsupportedAggregateOperationError,
    unknown_aggregate_field_message,
    unknown_group_by_field_message,
    unsupported_operation_message,
)
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    DimensionSpec,
    MetricSpec,
)
from public_api.aggregations.types import (
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
    TemporalGranularity,
)
from public_api.constants import PublicAPIResources


class FieldKind(enum.StrEnum):
    """What a registered field is, for the purpose of deciding its operations."""

    NUMERIC = "NUMERIC"
    STRING = "STRING"
    TEMPORAL = "TEMPORAL"
    BOOLEAN = "BOOLEAN"


AGGREGATE_TYPE_FOR_KIND: Mapping[FieldKind, type] = MappingProxyType(
    {
        FieldKind.NUMERIC: NumericAggregate,
        FieldKind.STRING: StringAggregate,
        FieldKind.TEMPORAL: DateTimeAggregate,
        FieldKind.BOOLEAN: BooleanAggregate,
    }
)

OPERATIONS_FOR_KIND: Mapping[FieldKind, tuple[AggregateOp, ...]] = MappingProxyType(
    {
        FieldKind.NUMERIC: (AggregateOp.SUM, AggregateOp.AVG, AggregateOp.MIN, AggregateOp.MAX),
        FieldKind.STRING: (AggregateOp.CONCAT, AggregateOp.MIN, AggregateOp.MAX),
        FieldKind.TEMPORAL: (AggregateOp.MIN, AggregateOp.MAX),
        FieldKind.BOOLEAN: (AggregateOp.TRUE_COUNT, AggregateOp.FALSE_COUNT),
    }
)

#: Alias every group carries. Not a registered field — every aggregate row has it.
COUNT_METRIC_NAME = "count"

#: Every metric alias starts here, so no alias can collide with a dimension
#: alias (a registry name, or one suffixed by a granularity) or a model field.
METRIC_ALIAS_PREFIX = "metric_"

#: The row-dict key the group's own ``COUNT(id)`` lands under.
COUNT_ALIAS = f"{METRIC_ALIAS_PREFIX}{COUNT_METRIC_NAME}"


def metric_alias(field_name: str, op: AggregateOp) -> str:
    """The row-dict key one (field, operation) pair is computed under.

    Canonical, so the selection set, a HAVING clause and an order-by all land on
    the same column instead of annotating the same aggregate two or three times
    under different names.
    """
    return f"{METRIC_ALIAS_PREFIX}{field_name}_{op.value.lower()}"


def relation_count_alias(relation_name: str) -> str:
    """The row-dict key a relation count is computed under."""
    return f"{METRIC_ALIAS_PREFIX}{relation_name}_count"


def concat_alias(field_name: str, index: int) -> str:
    """The row-dict key one ``concat`` selection is computed under.

    Indexed rather than named after its arguments: ``concat`` carries a
    separator and a distinctness flag, and two selections differing in either
    are two different columns on the same field.
    """
    return f"{METRIC_ALIAS_PREFIX}{field_name}_concat_{index}"


@dataclass(frozen=True, slots=True)
class MetricReference:
    """One metric a HAVING clause or an order-by may name.

    The referenceable set is deliberately the numbers: the group's own count,
    the four operations over each numeric field, and each relation count. A
    string or date-time aggregate is selectable but not referenceable — the
    comparisons this engine offers compare numbers, and offering an ordering a
    HAVING cannot express would be two notions of "a metric" rather than one.
    """

    name: str
    field_name: str
    op: AggregateOp
    alias: str

    def to_metric(self) -> MetricSpec:
        """The ``MetricSpec`` that computes this reference's column."""
        return MetricSpec(alias=self.alias, field_path=self.field_name, op=self.op)


def referenceable_metrics(entity: AggregatableEntity) -> Mapping[str, MetricReference]:
    """Every metric of an entity that a HAVING clause or an order-by may name.

    Keyed by the snake_case name the GraphQL enums carry in upper case.
    """
    registration = get_registration(entity)
    references: dict[str, MetricReference] = {
        COUNT_METRIC_NAME: MetricReference(
            name=COUNT_METRIC_NAME,
            field_name=COUNT_METRIC_NAME,
            op=AggregateOp.COUNT,
            alias=COUNT_ALIAS,
        )
    }
    for field_name, registered in registration.aggregatable.items():
        if registered.kind is not FieldKind.NUMERIC:
            continue
        for op in registered.operations:
            name = f"{field_name}_{op.value.lower()}"
            references[name] = MetricReference(
                name=name,
                field_name=field_name,
                op=op,
                alias=metric_alias(field_name, op),
            )
    for relation_name in registration.relation_counts:
        name = f"{relation_name}_count"
        references[name] = MetricReference(
            name=name,
            field_name=relation_name,
            op=AggregateOp.COUNT,
            alias=relation_count_alias(relation_name),
        )
    return MappingProxyType(references)


def _minutes_between(start_path: str, end_path: str) -> models.Expression:
    """Minutes from ``start_path`` to ``end_path`` as a float, computed in SQL.

    The three temporal entities store a start and an end but no duration column,
    and the plan forbids computing one in Python. ``EXTRACT(epoch FROM
    end - start) / 60`` is the ORM's way of saying it; the subtraction is
    wrapped so its output field is a duration rather than left to inference.
    """
    interval = models.ExpressionWrapper(
        models.F(end_path) - models.F(start_path), output_field=models.DurationField()
    )
    return models.ExpressionWrapper(
        Extract(interval, "epoch") / models.Value(60.0), output_field=models.FloatField()
    )


def _minutes_of(duration_path: str) -> models.Expression:
    """Minutes held by a ``DurationField`` column, as a float."""
    return models.ExpressionWrapper(
        Extract(models.F(duration_path), "epoch") / models.Value(60.0),
        output_field=models.FloatField(),
    )


@dataclass(frozen=True, slots=True)
class AggregatableField:
    """A field an entity can aggregate over.

    ``expression`` is set only for a derived metric — ``duration_minutes`` has
    no column behind it. When it is ``None`` the metric aggregates
    ``field_path`` directly.
    """

    name: str
    field_path: str
    kind: FieldKind
    expression: Callable[[], models.Expression] | None = None

    @property
    def aggregate_type(self) -> type:
        return AGGREGATE_TYPE_FOR_KIND[self.kind]

    @property
    def operations(self) -> tuple[AggregateOp, ...]:
        return OPERATIONS_FOR_KIND[self.kind]

    def supports(self, op: AggregateOp) -> bool:
        return op in self.operations


@dataclass(frozen=True, slots=True)
class GroupableField:
    """A field an entity can GROUP BY.

    ``field_path`` is the ORM path used in ``.values()``. For a foreign key it
    is the concrete ``<name>_fk_id`` column: a group key needs the id, and
    reading the local column joins nothing, so no organization clause can be
    dropped from a join that is never made. Scoping stays where it belongs, on
    the base queryset's manager.

    A ``TEMPORAL`` field is the only kind that accepts a granularity.
    """

    name: str
    field_path: str
    kind: FieldKind

    @property
    def is_temporal(self) -> bool:
        return self.kind is FieldKind.TEMPORAL


@dataclass(frozen=True, slots=True)
class RelationCountField:
    """A count of related rows, exposed as a metric on the parent entity.

    Counted through a ``Subquery`` rather than ``Count("relation__id")``.
    Several ``Count()`` calls over different relations in one ``annotate()``
    fan the join out and multiply each other's results — the bug
    ``public_api/queries.py`` already solves this way for ``childOrganizations``.

    ``related_model`` is queried through its own scoped manager, so the subquery
    carries the organization filter too.
    """

    name: str
    related_model: type[models.Model]
    parent_field_path: str


@dataclass(frozen=True, slots=True)
class EntityRegistration:
    """Everything the engine knows about one aggregatable entity."""

    entity: AggregatableEntity
    model: type[models.Model]
    resource: PublicAPIResources
    graphql_field_name: str
    groupable: Mapping[str, GroupableField] = field(default_factory=dict)
    aggregatable: Mapping[str, AggregatableField] = field(default_factory=dict)
    relation_counts: Mapping[str, RelationCountField] = field(default_factory=dict)

    def group_by_field(self, name: str) -> GroupableField:
        """Return the groupable field, or raise if the entity does not offer it."""
        try:
            return self.groupable[name]
        except KeyError:
            raise UnknownAggregateFieldError(unknown_group_by_field_message(name)) from None

    def metric_field(self, name: str) -> AggregatableField:
        """Return the aggregatable field, or raise if the entity does not offer it."""
        try:
            return self.aggregatable[name]
        except KeyError:
            raise UnknownAggregateFieldError(unknown_aggregate_field_message(name)) from None

    def assert_operation_supported(self, name: str, op: AggregateOp) -> AggregatableField:
        """Return the field, raising when the operation is wrong for its kind.

        This is the runtime half of the schema's promise. The schema already
        makes ``title { sum }`` unrepresentable; this catches an engine caller
        that built a ``MetricSpec`` by hand.
        """
        registered = self.metric_field(name)
        if not registered.supports(op):
            raise UnsupportedAggregateOperationError(unsupported_operation_message(name, op.value))
        return registered


def _temporal_range_fields() -> dict[str, AggregatableField]:
    """Aggregatable fields shared by the three start/end-bearing entities."""
    return {
        "start_time": AggregatableField("start_time", "start_time", FieldKind.TEMPORAL),
        "end_time": AggregatableField("end_time", "end_time", FieldKind.TEMPORAL),
        "duration_minutes": AggregatableField(
            "duration_minutes",
            "duration_minutes",
            FieldKind.NUMERIC,
            expression=lambda: _minutes_between("start_time", "end_time"),
        ),
        "timezone": AggregatableField("timezone", "timezone", FieldKind.STRING),
        "is_recurring_exception": AggregatableField(
            "is_recurring_exception", "is_recurring_exception", FieldKind.BOOLEAN
        ),
    }


def _timestamp_fields() -> dict[str, AggregatableField]:
    """``created`` / ``modified``, which every entity carries through ``BaseModel``."""
    return {
        "created": AggregatableField("created", "created", FieldKind.TEMPORAL),
        "modified": AggregatableField("modified", "modified", FieldKind.TEMPORAL),
    }


def _temporal_range_dimensions() -> dict[str, GroupableField]:
    """Group-by dimensions shared by the three start/end-bearing entities."""
    return {
        "start_time": GroupableField("start_time", "start_time", FieldKind.TEMPORAL),
        "end_time": GroupableField("end_time", "end_time", FieldKind.TEMPORAL),
        "timezone": GroupableField("timezone", "timezone", FieldKind.STRING),
        "is_recurring_exception": GroupableField(
            "is_recurring_exception", "is_recurring_exception", FieldKind.BOOLEAN
        ),
    }


def _created_dimension() -> dict[str, GroupableField]:
    return {"created": GroupableField("created", "created", FieldKind.TEMPORAL)}


_CALENDAR_EVENT = EntityRegistration(
    entity=AggregatableEntity.CALENDAR_EVENT,
    model=CalendarEvent,
    resource=PublicAPIResources.CALENDAR_EVENT,
    graphql_field_name="calendar_event_aggregate",
    groupable={
        "calendar_id": GroupableField("calendar_id", "calendar_fk_id", FieldKind.NUMERIC),
        "appointment_type_id": GroupableField(
            "appointment_type_id", "appointment_type_fk_id", FieldKind.NUMERIC
        ),
        "bundle_calendar_id": GroupableField(
            "bundle_calendar_id", "bundle_calendar_fk_id", FieldKind.NUMERIC
        ),
        "is_bundle_primary": GroupableField(
            "is_bundle_primary", "is_bundle_primary", FieldKind.BOOLEAN
        ),
        **_temporal_range_dimensions(),
        **_created_dimension(),
    },
    aggregatable={
        "title": AggregatableField("title", "title", FieldKind.STRING),
        "description": AggregatableField("description", "description", FieldKind.STRING),
        "is_bundle_primary": AggregatableField(
            "is_bundle_primary", "is_bundle_primary", FieldKind.BOOLEAN
        ),
        **_temporal_range_fields(),
        **_timestamp_fields(),
    },
    relation_counts={
        "attendances": RelationCountField("attendances", EventAttendance, "event_fk_id"),
        "external_attendances": RelationCountField(
            "external_attendances", EventExternalAttendance, "event_fk_id"
        ),
        "resource_allocations": RelationCountField(
            "resource_allocations", ResourceAllocation, "event_fk_id"
        ),
    },
)

_AVAILABLE_TIME = EntityRegistration(
    entity=AggregatableEntity.AVAILABLE_TIME,
    model=AvailableTime,
    resource=PublicAPIResources.AVAILABLE_TIME,
    graphql_field_name="available_time_aggregate",
    groupable={
        "calendar_id": GroupableField("calendar_id", "calendar_fk_id", FieldKind.NUMERIC),
        "appointment_type_slot_id": GroupableField(
            "appointment_type_slot_id", "appointment_type_slot_fk_id", FieldKind.NUMERIC
        ),
        **_temporal_range_dimensions(),
        **_created_dimension(),
    },
    aggregatable={**_temporal_range_fields(), **_timestamp_fields()},
)

_BLOCKED_TIME = EntityRegistration(
    entity=AggregatableEntity.BLOCKED_TIME,
    model=BlockedTime,
    resource=PublicAPIResources.BLOCKED_TIME,
    graphql_field_name="blocked_time_aggregate",
    groupable={
        "calendar_id": GroupableField("calendar_id", "calendar_fk_id", FieldKind.NUMERIC),
        "bundle_calendar_id": GroupableField(
            "bundle_calendar_id", "bundle_calendar_fk_id", FieldKind.NUMERIC
        ),
        "appointment_type_slot_id": GroupableField(
            "appointment_type_slot_id", "appointment_type_slot_fk_id", FieldKind.NUMERIC
        ),
        **_temporal_range_dimensions(),
        **_created_dimension(),
    },
    aggregatable={
        "reason": AggregatableField("reason", "reason", FieldKind.STRING),
        **_temporal_range_fields(),
        **_timestamp_fields(),
    },
)

_APPOINTMENT_TYPE = EntityRegistration(
    entity=AggregatableEntity.APPOINTMENT_TYPE,
    model=AppointmentType,
    resource=PublicAPIResources.APPOINTMENT_TYPE,
    graphql_field_name="appointment_type_aggregate",
    groupable={
        "accepts_public_scheduling": GroupableField(
            "accepts_public_scheduling", "accepts_public_scheduling", FieldKind.BOOLEAN
        ),
        **_created_dimension(),
    },
    aggregatable={
        "name": AggregatableField("name", "name", FieldKind.STRING),
        "description": AggregatableField("description", "description", FieldKind.STRING),
        "accepts_public_scheduling": AggregatableField(
            "accepts_public_scheduling", "accepts_public_scheduling", FieldKind.BOOLEAN
        ),
        "duration_minutes": AggregatableField(
            "duration_minutes",
            "duration_minutes",
            FieldKind.NUMERIC,
            expression=lambda: _minutes_of("duration"),
        ),
        **_timestamp_fields(),
    },
    relation_counts={
        "events": RelationCountField("events", CalendarEvent, "appointment_type_fk_id"),
        "slots": RelationCountField("slots", AppointmentTypeSlot, "appointment_type_fk_id"),
    },
)

_CALENDAR = EntityRegistration(
    entity=AggregatableEntity.CALENDAR,
    model=Calendar,
    resource=PublicAPIResources.CALENDAR,
    graphql_field_name="calendar_aggregate",
    groupable={
        "provider": GroupableField("provider", "provider", FieldKind.STRING),
        "calendar_type": GroupableField("calendar_type", "calendar_type", FieldKind.STRING),
        "visibility": GroupableField("visibility", "visibility", FieldKind.STRING),
        "sync_enabled": GroupableField("sync_enabled", "sync_enabled", FieldKind.BOOLEAN),
        "manage_available_windows": GroupableField(
            "manage_available_windows", "manage_available_windows", FieldKind.BOOLEAN
        ),
        "accepts_public_scheduling": GroupableField(
            "accepts_public_scheduling", "accepts_public_scheduling", FieldKind.BOOLEAN
        ),
        **_created_dimension(),
    },
    aggregatable={
        "name": AggregatableField("name", "name", FieldKind.STRING),
        "description": AggregatableField("description", "description", FieldKind.STRING),
        "email": AggregatableField("email", "email", FieldKind.STRING),
        "provider": AggregatableField("provider", "provider", FieldKind.STRING),
        "calendar_type": AggregatableField("calendar_type", "calendar_type", FieldKind.STRING),
        "visibility": AggregatableField("visibility", "visibility", FieldKind.STRING),
        "capacity": AggregatableField("capacity", "capacity", FieldKind.NUMERIC),
        "sync_enabled": AggregatableField("sync_enabled", "sync_enabled", FieldKind.BOOLEAN),
        "manage_available_windows": AggregatableField(
            "manage_available_windows", "manage_available_windows", FieldKind.BOOLEAN
        ),
        "accepts_public_scheduling": AggregatableField(
            "accepts_public_scheduling", "accepts_public_scheduling", FieldKind.BOOLEAN
        ),
        **_timestamp_fields(),
    },
    relation_counts={
        "events": RelationCountField("events", CalendarEvent, "calendar_fk_id"),
        "blocked_times": RelationCountField("blocked_times", BlockedTime, "calendar_fk_id"),
        "available_times": RelationCountField("available_times", AvailableTime, "calendar_fk_id"),
    },
)

_CALENDAR_POOL = EntityRegistration(
    entity=AggregatableEntity.CALENDAR_POOL,
    model=CalendarPool,
    resource=PublicAPIResources.CALENDAR_POOL,
    graphql_field_name="calendar_pool_aggregate",
    groupable=_created_dimension(),
    aggregatable={
        "name": AggregatableField("name", "name", FieldKind.STRING),
        "description": AggregatableField("description", "description", FieldKind.STRING),
        **_timestamp_fields(),
    },
    relation_counts={
        "memberships": RelationCountField("memberships", CalendarPoolMembership, "pool_fk_id"),
    },
)


REGISTRY: Mapping[AggregatableEntity, EntityRegistration] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: _CALENDAR_EVENT,
        AggregatableEntity.AVAILABLE_TIME: _AVAILABLE_TIME,
        AggregatableEntity.BLOCKED_TIME: _BLOCKED_TIME,
        AggregatableEntity.APPOINTMENT_TYPE: _APPOINTMENT_TYPE,
        AggregatableEntity.CALENDAR: _CALENDAR,
        AggregatableEntity.CALENDAR_POOL: _CALENDAR_POOL,
    }
)


def get_registration(entity: AggregatableEntity) -> EntityRegistration:
    """Return the registration for an entity, raising if it has none."""
    try:
        return REGISTRY[entity]
    except KeyError:
        raise UnknownAggregateEntityError(f"Unknown aggregate entity: {entity}") from None


def aggregate_type_for(kind: FieldKind) -> type:
    """Return the Strawberry type a field of this kind is exposed as."""
    return AGGREGATE_TYPE_FOR_KIND[kind]


def build_metric(
    entity: AggregatableEntity,
    field_name: str,
    op: AggregateOp,
    alias: str | None = None,
    **options: Any,
) -> MetricSpec:
    """Build one validated ``MetricSpec`` for an entity.

    Goes through the registry so an unknown field or a wrong operation raises
    here, at plan-construction time, rather than producing a queryset that
    quietly omits the metric.
    """
    registration = get_registration(entity)
    if op is AggregateOp.COUNT:
        if field_name not in registration.relation_counts and field_name != COUNT_METRIC_NAME:
            raise UnknownAggregateFieldError(unknown_aggregate_field_message(field_name))
    else:
        registration.assert_operation_supported(field_name, op)
    return MetricSpec(
        alias=alias or f"{field_name}_{op.value.lower()}",
        field_path=field_name,
        op=op,
        options=dict(options),
    )


def build_dimension(
    entity: AggregatableEntity,
    field_name: str,
    alias: str | None = None,
    granularity: TemporalGranularity | None = None,
    tzinfo: ZoneInfo | None = None,
) -> DimensionSpec:
    """Build one validated ``DimensionSpec`` for an entity.

    A granularity on a non-temporal field is refused here as well as by the
    schema, so a hand-built plan cannot ask for ``provider`` bucketed by week.
    A granularity without a timezone is refused too: "per day" has no answer
    until somebody names the clock, and defaulting to the process timezone would
    give one silently.

    A bucketed dimension gets a granularity-suffixed alias by default —
    ``start_time`` at ``DAY`` becomes ``start_time_day``. That is not cosmetic:
    the bucket is an expression, and Django refuses an expression alias that
    collides with a concrete model field name, which ``start_time`` is.
    """
    registration = get_registration(entity)
    registered = registration.group_by_field(field_name)
    if granularity is not None and not registered.is_temporal:
        raise UnsupportedAggregateOperationError(
            unsupported_operation_message(field_name, "granularity")
        )
    if granularity is not None and tzinfo is None:
        raise InvalidAggregatePlanError(BUCKETING_NEEDS_TIMEZONE_MESSAGE)

    if alias is None:
        alias = field_name if granularity is None else f"{field_name}_{granularity.value.lower()}"
    return DimensionSpec(
        alias=alias,
        field_path=registered.field_path,
        granularity=granularity,
        tzinfo=tzinfo,
    )
