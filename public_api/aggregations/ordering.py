"""Typed ORDER BY: order groups by their key or by a metric, per entity.

Scoped the same way ``having.py`` is: a caller may order by the row count,
by a relation count, or by a numeric metric's ``sum`` / ``avg`` / ``min`` /
``max``. String, datetime and boolean aggregates are not orderable in this
phase, for the same reason they are not HAVING-able -- the surface this phase
builds answers "top N by a number", and every field it exposes is one.

**A caller sets exactly one of 'key' or 'metric' per entry.** GraphQL has no
input unions, so -- the same shape ``groupBy`` uses -- the wrapper input
carries one nullable slot per variant, and :func:`resolve_order_by` refuses
an entry that sets both or neither. ``key`` reuses the entity's own
``*GroupByInput`` (the same shape ``groupBy`` itself takes): ordering by a
dimension means naming it the same way grouping by it does, not learning a
second vocabulary for the same field. ``metric`` takes one of a small,
per-entity enum, so the schema itself refuses an entry naming an aggregate
this engine cannot compute or one the entity does not expose.

**Ordering by a metric adds it to the plan.** Exactly like ``having.py``, a
caller can order by a metric the row selection never asked to see; the
metric is folded into ``AggregateQueryPlan.metrics`` the same way, before
the plan is ever built, so the executor annotates it like any other.

The tiebreak that keeps paging stable when multiple groups tie on the
requested ordering is not this module's job -- the executor's own
``_ordering_terms`` already appends the group key for every plan, built or
not by this module (see ``test_ties_break_on_the_group_key_so_paging_is_stable``).
"""

import enum
from collections.abc import Sequence
from types import MappingProxyType
from typing import Any, Protocol

import strawberry

from public_api.aggregations.dimensions import (
    AppointmentTypeGroupByInput,
    AvailableTimeGroupByInput,
    BlockedTimeGroupByInput,
    CalendarEventGroupByInput,
    CalendarGroupByInput,
    CalendarPoolGroupByInput,
    resolve_dimensions,
)
from public_api.aggregations.errors import OrderKeyNotGroupedError, OrderVariantError
from public_api.aggregations.plan import (
    ROW_COUNT_ALIAS,
    ROW_COUNT_FIELD_PATH,
    AggregatableEntity,
    AggregateOp,
    MetricSpec,
    OrderSpec,
    default_metric_alias,
)
from public_api.aggregations.plan import OrderDirection as PlanOrderDirection


@strawberry.enum(description="Sort direction for one ordering term.")
class AggregateOrderDirection(enum.Enum):
    ASC = "ASC"
    DESC = "DESC"


_DIRECTION_BY_GRAPHQL: MappingProxyType[AggregateOrderDirection, PlanOrderDirection] = (
    MappingProxyType(
        {
            AggregateOrderDirection.ASC: PlanOrderDirection.ASC,
            AggregateOrderDirection.DESC: PlanOrderDirection.DESC,
        }
    )
)


# ---------------------------------------------------------------------------
# Per-entity orderable-metric enums
# ---------------------------------------------------------------------------
#
# Every member's value is the alias the metric would carry in the row dict --
# the same one ``default_metric_alias`` / the row-count / relation-count
# conventions produce -- so resolving a member never needs a second lookup
# for the alias, only for the ``MetricSpec`` to annotate it with.


@strawberry.enum(description="Metrics a calendar event aggregate may be ordered by.")
class CalendarEventOrderableMetric(enum.Enum):
    COUNT = ROW_COUNT_ALIAS
    ATTENDANCE_COUNT = "attendance_count"
    EXTERNAL_ATTENDANCE_COUNT = "external_attendance_count"
    RESOURCE_ALLOCATION_COUNT = "resource_allocation_count"
    DURATION_MINUTES_SUM = default_metric_alias("duration_minutes", AggregateOp.SUM)
    DURATION_MINUTES_AVG = default_metric_alias("duration_minutes", AggregateOp.AVG)
    DURATION_MINUTES_MIN = default_metric_alias("duration_minutes", AggregateOp.MIN)
    DURATION_MINUTES_MAX = default_metric_alias("duration_minutes", AggregateOp.MAX)


@strawberry.enum(description="Metrics an available time aggregate may be ordered by.")
class AvailableTimeOrderableMetric(enum.Enum):
    COUNT = ROW_COUNT_ALIAS
    DURATION_MINUTES_SUM = default_metric_alias("duration_minutes", AggregateOp.SUM)
    DURATION_MINUTES_AVG = default_metric_alias("duration_minutes", AggregateOp.AVG)
    DURATION_MINUTES_MIN = default_metric_alias("duration_minutes", AggregateOp.MIN)
    DURATION_MINUTES_MAX = default_metric_alias("duration_minutes", AggregateOp.MAX)


@strawberry.enum(description="Metrics a blocked time aggregate may be ordered by.")
class BlockedTimeOrderableMetric(enum.Enum):
    COUNT = ROW_COUNT_ALIAS
    DURATION_MINUTES_SUM = default_metric_alias("duration_minutes", AggregateOp.SUM)
    DURATION_MINUTES_AVG = default_metric_alias("duration_minutes", AggregateOp.AVG)
    DURATION_MINUTES_MIN = default_metric_alias("duration_minutes", AggregateOp.MIN)
    DURATION_MINUTES_MAX = default_metric_alias("duration_minutes", AggregateOp.MAX)


@strawberry.enum(description="Metrics an appointment type aggregate may be ordered by.")
class AppointmentTypeOrderableMetric(enum.Enum):
    COUNT = ROW_COUNT_ALIAS
    EVENT_COUNT = "event_count"
    SLOT_COUNT = "slot_count"
    DURATION_MINUTES_SUM = default_metric_alias("duration_minutes", AggregateOp.SUM)
    DURATION_MINUTES_AVG = default_metric_alias("duration_minutes", AggregateOp.AVG)
    DURATION_MINUTES_MIN = default_metric_alias("duration_minutes", AggregateOp.MIN)
    DURATION_MINUTES_MAX = default_metric_alias("duration_minutes", AggregateOp.MAX)


@strawberry.enum(description="Metrics a calendar aggregate may be ordered by.")
class CalendarOrderableMetric(enum.Enum):
    COUNT = ROW_COUNT_ALIAS
    EVENT_COUNT = "event_count"
    BLOCKED_TIME_COUNT = "blocked_time_count"
    AVAILABLE_TIME_COUNT = "available_time_count"
    CAPACITY_SUM = default_metric_alias("capacity", AggregateOp.SUM)
    CAPACITY_AVG = default_metric_alias("capacity", AggregateOp.AVG)
    CAPACITY_MIN = default_metric_alias("capacity", AggregateOp.MIN)
    CAPACITY_MAX = default_metric_alias("capacity", AggregateOp.MAX)


@strawberry.enum(description="Metrics a calendar pool aggregate may be ordered by.")
class CalendarPoolOrderableMetric(enum.Enum):
    COUNT = ROW_COUNT_ALIAS
    CALENDAR_COUNT = "calendar_count"


#: Each orderable-metric member to the field path and operation it names --
#: ``None`` for the count-like members, whose field path is the relation /
#: row-count key itself with a plain ``COUNT``.
_NUMERIC_SUFFIX_TO_OP: MappingProxyType[str, AggregateOp] = MappingProxyType(
    {
        "sum": AggregateOp.SUM,
        "avg": AggregateOp.AVG,
        "min": AggregateOp.MIN,
        "max": AggregateOp.MAX,
    }
)


def _metric_spec_for(
    member: enum.Enum, count_like_aliases: frozenset[str]
) -> tuple[str, str, AggregateOp]:
    """The ``(alias, field_path, op)`` a member's value describes.

    A count-like alias (``count`` itself, or a relation count) IS its own
    field path with :data:`AggregateOp.COUNT`. Anything else is
    ``<field_path>__<op>``, the same convention ``default_metric_alias``
    produces, so it is parsed back rather than tabled twice.
    """
    alias = member.value
    if alias in count_like_aliases:
        field_path = ROW_COUNT_FIELD_PATH if alias == ROW_COUNT_ALIAS else alias
        return alias, field_path, AggregateOp.COUNT
    field_path, _, suffix = alias.rpartition("__")
    return alias, field_path, _NUMERIC_SUFFIX_TO_OP[suffix]


# ---------------------------------------------------------------------------
# Per-entity ORDER BY inputs
# ---------------------------------------------------------------------------

_ORDER_INPUT_DESCRIPTION = (
    "Order groups by their key or by a metric. Set exactly one of 'key' or 'metric'."
)


@strawberry.input(description=_ORDER_INPUT_DESCRIPTION)
class CalendarEventAggregateOrderInput:
    key: CalendarEventGroupByInput | None = None
    metric: CalendarEventOrderableMetric | None = None
    direction: AggregateOrderDirection = AggregateOrderDirection.DESC


@strawberry.input(description=_ORDER_INPUT_DESCRIPTION)
class AvailableTimeAggregateOrderInput:
    key: AvailableTimeGroupByInput | None = None
    metric: AvailableTimeOrderableMetric | None = None
    direction: AggregateOrderDirection = AggregateOrderDirection.DESC


@strawberry.input(description=_ORDER_INPUT_DESCRIPTION)
class BlockedTimeAggregateOrderInput:
    key: BlockedTimeGroupByInput | None = None
    metric: BlockedTimeOrderableMetric | None = None
    direction: AggregateOrderDirection = AggregateOrderDirection.DESC


@strawberry.input(description=_ORDER_INPUT_DESCRIPTION)
class AppointmentTypeAggregateOrderInput:
    key: AppointmentTypeGroupByInput | None = None
    metric: AppointmentTypeOrderableMetric | None = None
    direction: AggregateOrderDirection = AggregateOrderDirection.DESC


@strawberry.input(description=_ORDER_INPUT_DESCRIPTION)
class CalendarAggregateOrderInput:
    key: CalendarGroupByInput | None = None
    metric: CalendarOrderableMetric | None = None
    direction: AggregateOrderDirection = AggregateOrderDirection.DESC


@strawberry.input(description=_ORDER_INPUT_DESCRIPTION)
class CalendarPoolAggregateOrderInput:
    key: CalendarPoolGroupByInput | None = None
    metric: CalendarPoolOrderableMetric | None = None
    direction: AggregateOrderDirection = AggregateOrderDirection.DESC


#: Per-entity order input, keyed the same way every other per-entity lookup
#: table in this package is.
ORDER_INPUT_BY_ENTITY: MappingProxyType[AggregatableEntity, type] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: CalendarEventAggregateOrderInput,
        AggregatableEntity.AVAILABLE_TIME: AvailableTimeAggregateOrderInput,
        AggregatableEntity.BLOCKED_TIME: BlockedTimeAggregateOrderInput,
        AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeAggregateOrderInput,
        AggregatableEntity.CALENDAR: CalendarAggregateOrderInput,
        AggregatableEntity.CALENDAR_POOL: CalendarPoolAggregateOrderInput,
    }
)

#: Every entity's count-like orderable-metric aliases -- the row count plus
#: its relation counts -- as the set :func:`_metric_spec_for` checks against.
_COUNT_LIKE_ALIASES_BY_ENTITY: MappingProxyType[AggregatableEntity, frozenset[str]] = (
    MappingProxyType(
        {
            AggregatableEntity.CALENDAR_EVENT: frozenset(
                {
                    ROW_COUNT_ALIAS,
                    "attendance_count",
                    "external_attendance_count",
                    "resource_allocation_count",
                }
            ),
            AggregatableEntity.AVAILABLE_TIME: frozenset({ROW_COUNT_ALIAS}),
            AggregatableEntity.BLOCKED_TIME: frozenset({ROW_COUNT_ALIAS}),
            AggregatableEntity.APPOINTMENT_TYPE: frozenset(
                {ROW_COUNT_ALIAS, "event_count", "slot_count"}
            ),
            AggregatableEntity.CALENDAR: frozenset(
                {
                    ROW_COUNT_ALIAS,
                    "event_count",
                    "blocked_time_count",
                    "available_time_count",
                }
            ),
            AggregatableEntity.CALENDAR_POOL: frozenset({ROW_COUNT_ALIAS, "calendar_count"}),
        }
    )
)


class OrderEntry(Protocol):
    """The shape every entity's order-by wrapper input has.

    Structural, like ``dimensions.GroupByEntry`` -- each entity's ``key`` and
    ``metric`` slots are typed to its own inputs/enums, so there is nothing
    to inherit but this contract.
    """

    key: Any
    metric: Any
    direction: AggregateOrderDirection


def resolve_order_by(
    entity: AggregatableEntity,
    order_by: Sequence[OrderEntry],
    timezone_name: str,
    dimension_aliases: Sequence[str],
) -> tuple[tuple[OrderSpec, ...], tuple[Any, ...]]:
    """An ``orderBy`` argument, as plan-ready ``OrderSpec``s plus the extra
    metrics a metric-ordered entry needs.

    ``timezone_name`` is only ever consulted for a ``key`` entry naming a
    temporal dimension, and only to compute its alias -- the same one
    ``groupBy`` would have produced for the identical entry, via the same
    :func:`~public_api.aggregations.dimensions.resolve_dimensions`, so a
    caller ordering by the dimension it actually grouped by always lands on
    a matching alias.

    ``dimension_aliases`` is that query's own resolved ``groupBy`` aliases.
    ``AggregateQueryPlan`` would itself refuse a ``key`` alias outside that
    set, but with ``InvalidPlanError`` -- an engine-internal exception, not a
    ``GraphQLError`` -- since that check exists to catch a plan built
    directly in code. Ordering by a dimension the query did not group by is
    the same mistake reached through a real query, so it is refused here,
    before a plan is ever built, with :class:`OrderKeyNotGroupedError`.
    """
    count_like = _COUNT_LIKE_ALIASES_BY_ENTITY[entity]
    known_dimension_aliases = set(dimension_aliases)
    order_specs: list[OrderSpec] = []
    extra_metrics: list[MetricSpec] = []

    for entry in order_by:
        if (entry.key is None) == (entry.metric is None):
            raise OrderVariantError()

        direction = _DIRECTION_BY_GRAPHQL[entry.direction]

        if entry.key is not None:
            alias = resolve_dimensions([entry.key], timezone_name)[0].alias
            if alias not in known_dimension_aliases:
                raise OrderKeyNotGroupedError()
            order_specs.append(OrderSpec(alias=alias, direction=direction))
            continue

        alias, field_path, op = _metric_spec_for(entry.metric, count_like)
        extra_metrics.append(MetricSpec(alias=alias, field_path=field_path, op=op))
        order_specs.append(OrderSpec(alias=alias, direction=direction))

    return tuple(order_specs), tuple(extra_metrics)
