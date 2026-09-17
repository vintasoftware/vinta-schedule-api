"""The one place that decides what each entity may be grouped by and aggregated over.

Two mappings live here and nowhere else:

1. **Model field kind -> aggregate kind -> GraphQL type and allowed operations.**
   ``title`` is a ``CharField``, so it is ``STRING``, so it gets
   ``StringAggregate`` and the operations ``concat`` / ``min`` / ``max``, and
   never ``sum``. That chain is data, not a series of ``if`` statements spread
   across six resolvers.
2. **The per-entity registration**: which fields are groupable, which are
   aggregatable, and which reverse relations may be counted.

Each registration is checked against the real model at import time: a declared
kind that disagrees with the column's own type raises immediately, so the table
above cannot drift away from the schema it describes without the process
failing to start.

Tenancy is not this module's business and must not become it. A registration
names field paths; the queryset those paths are resolved against arrives from
the caller and comes from the model's organization-scoped manager. Nothing here
calls ``original_manager`` or ``unscoped()``.
"""

import enum
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from types import MappingProxyType
from typing import Any

from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import DurationField, ExpressionWrapper, F, FloatField, Value
from django.db.models.expressions import Combinable
from django.db.models.functions import Extract

from calendar_integration.models import (
    AppointmentType,
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarPool,
)
from public_api.aggregations.errors import (
    AggregateRegistrationError,
    UnknownAggregateEntityError,
    UnknownAggregateFieldError,
    UnknownDimensionError,
    UnsupportedAggregateOperationError,
)
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
)
from public_api.aggregations.types import (
    DEFAULT_CONCAT_SEPARATOR,
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
    TemporalGranularity,
)


class AggregateKind(enum.Enum):
    """The four families of aggregatable field, one per GraphQL output type."""

    NUMERIC = "numeric"
    STRING = "string"
    DATETIME = "datetime"
    BOOLEAN = "boolean"


# Which operations each kind offers. This is the schema-level guarantee the
# plan's Guiding Decisions rest on: strings are not summable, instants are not
# averageable, and neither fact is enforced by a resolver.
OPS_BY_KIND: Mapping[AggregateKind, frozenset[AggregateOp]] = MappingProxyType(
    {
        AggregateKind.NUMERIC: frozenset(
            {AggregateOp.SUM, AggregateOp.AVG, AggregateOp.MIN, AggregateOp.MAX}
        ),
        AggregateKind.STRING: frozenset({AggregateOp.CONCAT, AggregateOp.MIN, AggregateOp.MAX}),
        AggregateKind.DATETIME: frozenset({AggregateOp.MIN, AggregateOp.MAX}),
        AggregateKind.BOOLEAN: frozenset({AggregateOp.TRUE_COUNT, AggregateOp.FALSE_COUNT}),
    }
)

GRAPHQL_TYPE_BY_KIND: Mapping[AggregateKind, type] = MappingProxyType(
    {
        AggregateKind.NUMERIC: NumericAggregate,
        AggregateKind.STRING: StringAggregate,
        AggregateKind.DATETIME: DateTimeAggregate,
        AggregateKind.BOOLEAN: BooleanAggregate,
    }
)

# Most specific first: ``DateTimeField`` is a ``DateField``, ``EmailField`` is a
# ``CharField``, and ``NaiveDateTimeField`` is a ``DateTimeField``. ``BooleanField``
# leads because a boolean read as a number would offer ``avg``.
_KIND_BY_FIELD_CLASS: Sequence[tuple[type[models.Field], AggregateKind]] = (
    (models.BooleanField, AggregateKind.BOOLEAN),
    (models.DurationField, AggregateKind.NUMERIC),
    (models.IntegerField, AggregateKind.NUMERIC),
    (models.FloatField, AggregateKind.NUMERIC),
    (models.DecimalField, AggregateKind.NUMERIC),
    (models.DateTimeField, AggregateKind.DATETIME),
    (models.DateField, AggregateKind.DATETIME),
    (models.TextField, AggregateKind.STRING),
    (models.CharField, AggregateKind.STRING),
)


def concrete_output_field(model_field: models.Field) -> models.Field | None:
    """The field whose *type* decides how a column aggregates.

    A ``GeneratedField`` (``start_time`` / ``end_time``) is a column whose type
    is its ``output_field``; aggregating one is no different from aggregating a
    stored column.
    """
    if isinstance(model_field, models.GeneratedField):
        return model_field.output_field
    return model_field


def aggregate_kind_for_model_field(model_field: models.Field) -> AggregateKind | None:
    """Map a concrete model field onto its aggregate kind, or ``None``.

    ``None`` means "this column has no sensible aggregate" -- a JSON blob, a
    file, a relation. Returning ``None`` rather than guessing keeps the
    registry's import-time check honest: a field nobody classified cannot be
    silently admitted as numeric.
    """
    resolved = concrete_output_field(model_field)
    if resolved is None:
        return None

    for field_class, kind in _KIND_BY_FIELD_CLASS:
        if isinstance(resolved, field_class):
            return kind
    return None


# What each operation does when its options are not named. An option equal to
# its default contributes nothing to a metric's alias, so the common selection
# keeps the short row key.
DEFAULT_METRIC_OPTIONS: Mapping[AggregateOp, Mapping[str, Any]] = MappingProxyType(
    {
        AggregateOp.COUNT: MappingProxyType({"distinct": False}),
        AggregateOp.CONCAT: MappingProxyType(
            {"separator": DEFAULT_CONCAT_SEPARATOR, "distinct": False}
        ),
    }
)


def metric_alias(field_name: str, op: AggregateOp, options: Mapping[str, Any] | None = None) -> str:
    """The row key a metric lands under: ``title`` + ``min`` -> ``title_min``.

    Every phase names metrics through this function, for three reasons. It
    makes a row key predictable from the selection that asked for it; it keeps
    the key clear of the model's own column names -- Django refuses an
    annotation whose alias matches a field, so a metric plainly called ``title``
    would fail at query-build time; and it separates two selections of the *same*
    operation that carry different arguments.

    That last one is not hypothetical. ``concat`` takes a separator and a
    distinctness flag, and one legal GraphQL document may select it twice under
    two response keys::

        commas: concat(separator: ",")
        lines:  concat(separator: "\\n", distinct: true)

    Both are ``StringAgg`` over ``title`` and both would otherwise be named
    ``title_concat`` -- one row key for two different strings. Options that
    differ from :data:`DEFAULT_METRIC_OPTIONS` therefore extend the alias:
    ``distinct`` by name, because it reads, and anything else by a short stable
    digest, because a separator is arbitrary text and cannot be one.
    """
    parts = [field_name, op.value]
    extras = _non_default_options(op, options)
    if extras.pop("distinct", False):
        parts.append("distinct")
    if extras:
        parts.append(_options_digest(extras))
    return "_".join(parts)


def _non_default_options(op: AggregateOp, options: Mapping[str, Any] | None) -> dict[str, Any]:
    """The options that actually change this operation's SQL."""
    defaults = DEFAULT_METRIC_OPTIONS.get(op, {})
    return {
        key: value
        for key, value in (options or {}).items()
        if key not in defaults or defaults[key] != value
    }


def _options_digest(options: Mapping[str, Any]) -> str:
    """A short, stable key for a set of option values.

    ``blake2s`` rather than :func:`hash`, which is salted per process: a row key
    has to be the same one this query built it under and the next one too.
    """
    payload = repr(sorted(options.items())).encode()
    return hashlib.blake2s(payload, digest_size=4).hexdigest()


def dimension_alias(field_name: str, granularity: TemporalGranularity | None = None) -> str:
    """The row key a group-by dimension lands under.

    A bucketed dimension carries its bucket size (``start_time_day``) so two
    granularities of the same field can coexist in one plan.
    """
    if granularity is None:
        return field_name
    return f"{field_name}_{granularity.value.lower()}"


@dataclass(frozen=True)
class AggregatableField:
    """One field a caller may aggregate, and the single kind it may be read as.

    ``expression`` is set for a *derived* metric -- one with no column of its
    own, such as an event's length in minutes. When it is set the declared
    ``kind`` is taken at face value, because there is no column to check it
    against; when it is not, the kind is verified against the model at import.
    """

    name: str
    kind: AggregateKind
    field_path: str = ""
    expression: Combinable | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not self.field_path:
            object.__setattr__(self, "field_path", self.name)

    @property
    def ops(self) -> frozenset[AggregateOp]:
        """The operations this field offers, from its kind."""
        return OPS_BY_KIND[self.kind]

    @property
    def graphql_type(self) -> type:
        """The one aggregate output type this field is exposed as."""
        return GRAPHQL_TYPE_BY_KIND[self.kind]


@dataclass(frozen=True)
class GroupableField:
    """One dimension a caller may group by.

    ``temporal`` marks the fields that accept a ``granularity``; every other
    dimension is grouped on its raw value.
    """

    name: str
    field_path: str = ""
    temporal: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if not self.field_path:
            object.__setattr__(self, "field_path", self.name)


@dataclass(frozen=True)
class RelationCount:
    """A count of related rows, counted through a correlated subquery.

    Combining several ``Count()`` calls over different relations in one
    ``annotate()`` fans the join out and multiplies every count by the others'
    row counts. The fix already used for ``childOrganizations`` in
    ``public_api/queries.py`` is to count each relation in its own subquery, and
    this is the registration that lets the executor do the same thing
    generically: ``relation`` is the reverse accessor on the aggregated model.
    """

    name: str
    relation: str
    description: str = ""


@dataclass(frozen=True)
class EntityRegistration:
    """Everything the engine knows about one aggregatable entity."""

    entity: AggregatableEntity
    model: type[models.Model]
    groupable: tuple[GroupableField, ...]
    aggregatable: tuple[AggregatableField, ...]
    relation_counts: tuple[RelationCount, ...] = ()

    _groupable_by_name: Mapping[str, GroupableField] = dataclass_field(
        default_factory=dict, repr=False, compare=False
    )
    _aggregatable_by_name: Mapping[str, AggregatableField] = dataclass_field(
        default_factory=dict, repr=False, compare=False
    )
    _relation_counts_by_name: Mapping[str, RelationCount] = dataclass_field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_groupable_by_name",
            MappingProxyType({spec.name: spec for spec in self.groupable}),
        )
        object.__setattr__(
            self,
            "_aggregatable_by_name",
            MappingProxyType({spec.name: spec for spec in self.aggregatable}),
        )
        object.__setattr__(
            self,
            "_relation_counts_by_name",
            MappingProxyType({spec.name: spec for spec in self.relation_counts}),
        )
        self._verify_against_model()

    def _verify_against_model(self) -> None:
        """Fail at import if this table disagrees with the model it describes.

        Three things are checked: every declared path resolves, every declared
        kind matches the column's own type, and every relation count names a
        real reverse relation. Checking here rather than at query time is what
        makes "one place decides" true -- a field renamed on the model breaks
        the process rather than one partner's query.
        """
        for dimension in self.groupable:
            self._resolve_model_field(dimension.field_path, dimension.name)

        for metric in self.aggregatable:
            if metric.expression is not None:
                # Derived metric: there is no column whose type could confirm
                # or contradict the declared kind.
                continue
            model_field = self._resolve_model_field(metric.field_path, metric.name)
            if isinstance(concrete_output_field(model_field), models.DurationField):
                # A duration column *is* numeric, and the kind map says so --
                # but ``Sum``/``Avg``/``Min``/``Max`` over an interval return a
                # ``timedelta``, and ``NumericAggregate`` publishes floats. The
                # column has to be read through an expression that yields a
                # number, which is what both duration metrics registered below
                # already do. Refused here so the next one cannot forget.
                raise AggregateRegistrationError(
                    f"{self.model.__name__}.{metric.field_path} is a DurationField; register "
                    f"{metric.name!r} with an `expression` yielding a number (minutes, say) -- "
                    f"aggregating an interval returns a timedelta, which NumericAggregate "
                    f"cannot publish"
                )
            actual = aggregate_kind_for_model_field(model_field)
            if actual is None:
                raise AggregateRegistrationError(
                    f"{self.model.__name__}.{metric.field_path} has no aggregate kind; "
                    f"{metric.name!r} cannot be registered as aggregatable"
                )
            if actual is not metric.kind:
                raise AggregateRegistrationError(
                    f"{self.model.__name__}.{metric.field_path} is {actual.value}, but "
                    f"{metric.name!r} is registered as {metric.kind.value}"
                )

        for relation in self.relation_counts:
            reverse_relation_fk_attname(self.model, relation.relation)

    def _resolve_model_field(self, field_path: str, name: str) -> models.Field:
        try:
            resolved = self.model._meta.get_field(field_path)
        except FieldDoesNotExist as exc:
            raise AggregateRegistrationError(
                f"{self.model.__name__} has no field {field_path!r} (registered as {name!r})"
            ) from exc
        if not isinstance(resolved, models.Field):
            raise AggregateRegistrationError(
                f"{self.model.__name__}.{field_path} is not a concrete field "
                f"(registered as {name!r})"
            )
        return resolved

    def groupable_field(self, name: str) -> GroupableField:
        """Look up a dimension, raising rather than returning ``None``."""
        try:
            return self._groupable_by_name[name]
        except KeyError:
            raise UnknownDimensionError(self.entity, name) from None

    def aggregatable_field(self, name: str) -> AggregatableField:
        """Look up an aggregatable field, raising rather than returning ``None``."""
        try:
            return self._aggregatable_by_name[name]
        except KeyError:
            raise UnknownAggregateFieldError(self.entity, name) from None

    def relation_count(self, name: str) -> RelationCount | None:
        """The relation count registered under ``name``, or ``None``."""
        return self._relation_counts_by_name.get(name)

    def supports(self, name: str, op: AggregateOp) -> bool:
        """Whether ``name`` offers ``op``, without raising on an unknown name."""
        spec = self._aggregatable_by_name.get(name)
        return spec is not None and op in spec.ops


def reverse_relation_fk_attname(model: type[models.Model], relation: str) -> str:
    """The column on the related model that points back at ``model``.

    ``OrganizationSafeForeignKey`` declares *two* fields: a concrete
    ``<name>_fk`` holding the column, and an organization-matched
    ``ForeignObject`` named ``<name>`` used for joins. The reverse accessor
    resolves to the second, which has no single column to filter on, so the
    concrete one is looked up by the ``_fk`` suffix the package guarantees.

    Filtering a subquery on that column rather than traversing the safe
    relation does not widen anything: the subquery runs through the related
    model's own organization-scoped manager, so the organization is in its
    ``WHERE`` clause. It is the join's ``ON`` clause that must never lose the
    organization, and there is no join here.
    """
    try:
        rel = model._meta.get_field(relation)
    except FieldDoesNotExist as exc:
        raise AggregateRegistrationError(f"{model.__name__} has no relation {relation!r}") from exc

    remote_field = getattr(rel, "field", None)
    related_model = rel.related_model
    if remote_field is None or related_model is None:
        raise AggregateRegistrationError(
            f"{model.__name__}.{relation} is not a reverse foreign key"
        )

    if getattr(remote_field, "concrete", False):
        return str(remote_field.attname)

    try:
        concrete = related_model._meta.get_field(f"{remote_field.name}_fk")
    except FieldDoesNotExist as exc:
        raise AggregateRegistrationError(
            f"{related_model.__name__} has no concrete column behind the "
            f"organization-safe relation {remote_field.name!r}"
        ) from exc
    if not isinstance(concrete, models.Field):
        raise AggregateRegistrationError(
            f"{related_model.__name__}.{remote_field.name}_fk is not a concrete field"
        )
    return str(concrete.attname)


# ---------------------------------------------------------------------------
# Derived metric expressions
# ---------------------------------------------------------------------------
#
# ``duration_minutes`` is the numeric metric the plan's API design names on the
# event row type. No column holds it: an occupied span is ``end_time -
# start_time``, and both of those are the generated, timezone-correct columns
# rather than the naive wall-clock ones, so the subtraction is an interval
# between real instants and survives a DST boundary.

_ELAPSED_MINUTES = ExpressionWrapper(
    Extract(
        ExpressionWrapper(F("end_time") - F("start_time"), output_field=DurationField()),
        "epoch",
    )
    / Value(60.0),
    output_field=FloatField(),
)

# ``AppointmentType.duration`` *is* a column, but a ``DurationField`` aggregates
# to a ``timedelta``, and ``NumericAggregate`` publishes floats. Reading it as
# minutes keeps one numeric contract across every entity.
_APPOINTMENT_DURATION_MINUTES = ExpressionWrapper(
    Extract(F("duration"), "epoch") / Value(60.0),
    output_field=FloatField(),
)


_CREATED_MODIFIED_DIMENSIONS = (
    GroupableField(name="created", temporal=True),
    GroupableField(name="modified", temporal=True),
)
_CREATED_MODIFIED_METRICS = (
    AggregatableField(name="created", kind=AggregateKind.DATETIME),
    AggregatableField(name="modified", kind=AggregateKind.DATETIME),
)

# A recurring row's span dimensions. ``start_time`` / ``end_time`` are the
# generated columns, which is what makes "per day in America/Sao_Paulo" answerable
# at all -- the editable ``*_tz_unaware`` columns are wall-clock readings whose
# comparison across timezones is meaningless.
_SPAN_DIMENSIONS = (
    GroupableField(name="start_time", temporal=True),
    GroupableField(name="end_time", temporal=True),
    GroupableField(name="timezone"),
    GroupableField(name="is_recurring_exception"),
)
_SPAN_METRICS = (
    AggregatableField(name="start_time", kind=AggregateKind.DATETIME),
    AggregatableField(name="end_time", kind=AggregateKind.DATETIME),
    AggregatableField(name="is_recurring_exception", kind=AggregateKind.BOOLEAN),
    AggregatableField(
        name="duration_minutes",
        kind=AggregateKind.NUMERIC,
        expression=_ELAPSED_MINUTES,
        description="Length of the row's span in minutes.",
    ),
)


_REGISTRATIONS: tuple[EntityRegistration, ...] = (
    EntityRegistration(
        entity=AggregatableEntity.CALENDAR_EVENT,
        model=CalendarEvent,
        groupable=(
            GroupableField(name="calendar_id", field_path="calendar_fk"),
            GroupableField(name="appointment_type_id", field_path="appointment_type_fk"),
            GroupableField(name="bundle_calendar_id", field_path="bundle_calendar_fk"),
            GroupableField(name="is_bundle_primary"),
            *_SPAN_DIMENSIONS,
            *_CREATED_MODIFIED_DIMENSIONS,
        ),
        aggregatable=(
            AggregatableField(name="title", kind=AggregateKind.STRING),
            AggregatableField(name="description", kind=AggregateKind.STRING),
            AggregatableField(name="is_bundle_primary", kind=AggregateKind.BOOLEAN),
            *_SPAN_METRICS,
            *_CREATED_MODIFIED_METRICS,
        ),
        relation_counts=(
            RelationCount(name="attendance_count", relation="attendances"),
            RelationCount(name="external_attendance_count", relation="external_attendances"),
            RelationCount(name="resource_allocation_count", relation="resource_allocations"),
        ),
    ),
    EntityRegistration(
        entity=AggregatableEntity.AVAILABLE_TIME,
        model=AvailableTime,
        groupable=(
            GroupableField(name="calendar_id", field_path="calendar_fk"),
            *_SPAN_DIMENSIONS,
            *_CREATED_MODIFIED_DIMENSIONS,
        ),
        aggregatable=(
            *_SPAN_METRICS,
            *_CREATED_MODIFIED_METRICS,
        ),
    ),
    EntityRegistration(
        entity=AggregatableEntity.BLOCKED_TIME,
        model=BlockedTime,
        groupable=(
            GroupableField(name="calendar_id", field_path="calendar_fk"),
            GroupableField(name="bundle_calendar_id", field_path="bundle_calendar_fk"),
            *_SPAN_DIMENSIONS,
            *_CREATED_MODIFIED_DIMENSIONS,
        ),
        aggregatable=(
            AggregatableField(name="reason", kind=AggregateKind.STRING),
            *_SPAN_METRICS,
            *_CREATED_MODIFIED_METRICS,
        ),
    ),
    EntityRegistration(
        entity=AggregatableEntity.APPOINTMENT_TYPE,
        model=AppointmentType,
        groupable=(
            GroupableField(name="accepts_public_scheduling"),
            *_CREATED_MODIFIED_DIMENSIONS,
        ),
        aggregatable=(
            AggregatableField(name="name", kind=AggregateKind.STRING),
            AggregatableField(name="description", kind=AggregateKind.STRING),
            AggregatableField(name="accepts_public_scheduling", kind=AggregateKind.BOOLEAN),
            AggregatableField(
                name="duration_minutes",
                kind=AggregateKind.NUMERIC,
                expression=_APPOINTMENT_DURATION_MINUTES,
                description="The pinned booking length in minutes, when one is set.",
            ),
            *_CREATED_MODIFIED_METRICS,
        ),
        relation_counts=(
            RelationCount(name="event_count", relation="events"),
            RelationCount(name="slot_count", relation="slots"),
        ),
    ),
    EntityRegistration(
        entity=AggregatableEntity.CALENDAR,
        model=Calendar,
        groupable=(
            GroupableField(name="calendar_type"),
            GroupableField(name="provider"),
            GroupableField(name="visibility"),
            GroupableField(name="sync_enabled"),
            GroupableField(name="accepts_public_scheduling"),
            GroupableField(name="manage_available_windows"),
            *_CREATED_MODIFIED_DIMENSIONS,
        ),
        aggregatable=(
            AggregatableField(name="name", kind=AggregateKind.STRING),
            AggregatableField(name="description", kind=AggregateKind.STRING),
            AggregatableField(name="capacity", kind=AggregateKind.NUMERIC),
            AggregatableField(name="sync_enabled", kind=AggregateKind.BOOLEAN),
            AggregatableField(name="accepts_public_scheduling", kind=AggregateKind.BOOLEAN),
            AggregatableField(name="manage_available_windows", kind=AggregateKind.BOOLEAN),
            *_CREATED_MODIFIED_METRICS,
        ),
        relation_counts=(
            RelationCount(name="event_count", relation="events"),
            RelationCount(name="blocked_time_count", relation="blocked_times"),
            RelationCount(name="available_time_count", relation="available_times"),
        ),
    ),
    EntityRegistration(
        entity=AggregatableEntity.CALENDAR_POOL,
        model=CalendarPool,
        groupable=_CREATED_MODIFIED_DIMENSIONS,
        aggregatable=(
            AggregatableField(name="name", kind=AggregateKind.STRING),
            AggregatableField(name="description", kind=AggregateKind.STRING),
            *_CREATED_MODIFIED_METRICS,
        ),
        relation_counts=(RelationCount(name="membership_count", relation="memberships"),),
    ),
)


REGISTRY: Mapping[AggregatableEntity, EntityRegistration] = MappingProxyType(
    {registration.entity: registration for registration in _REGISTRATIONS}
)


def get_registration(entity: AggregatableEntity) -> EntityRegistration:
    """The registration for ``entity``, raising rather than returning ``None``."""
    try:
        return REGISTRY[entity]
    except KeyError:
        raise UnknownAggregateEntityError(entity) from None


def validate_plan(plan: AggregateQueryPlan) -> EntityRegistration:
    """Check a plan against the registry and return the registration it names.

    Kept separate from :class:`AggregateQueryPlan`'s own validation because
    this half needs the models and that half must not: a plan is built, audited
    and compared without ever importing ``calendar_integration``.
    """
    registration = get_registration(plan.entity)

    for dimension in plan.dimensions:
        groupable = registration.groupable_field(dimension.field_path)
        if dimension.granularity is not None and not groupable.temporal:
            raise UnknownDimensionError(
                plan.entity, f"{dimension.field_path} (not a temporal dimension)"
            )

    for metric in plan.metrics:
        if metric.op is AggregateOp.COUNT:
            _validate_count_metric(registration, metric.field_path)
            continue
        aggregatable = registration.aggregatable_field(metric.field_path)
        if metric.op not in aggregatable.ops:
            raise UnsupportedAggregateOperationError(plan.entity, metric.field_path, metric.op)

    return registration


def _validate_count_metric(registration: EntityRegistration, field_path: str) -> None:
    """``COUNT`` is either the group's row count or a registered relation count."""
    if field_path in {"id", "pk"}:
        return
    if registration.relation_count(field_path) is not None:
        return
    raise UnknownAggregateFieldError(registration.entity, field_path)
