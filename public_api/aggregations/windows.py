"""Typed window functions over the already-grouped result, per entity.

A window reads one metric -- the group's ``count``, a numeric field's
``sum``, a relation count -- and computes four things over the rows the
``GROUP BY`` produced: a running total, a moving average, a rank, and each
row's share of its partition. All four are ``OVER (...)`` clauses in the same
statement as the grouping, so nothing accumulates in Python and nothing needs
a second query.

**The frame shapes the moving average and nothing else.** A running total is
cumulative from the start of its partition by definition, and a rank and a
share of a partition read no frame at all. Sharing one frame between them
would mean a query asking for a three-row moving average silently got a
three-row rolling sum labelled ``runningTotal``; keeping the frame to the one
function whose meaning depends on it is what lets a single query carry both.

**A window's ordering is not the result's ordering.** ``orderBy`` on the
field decides what order the caller reads rows in; ``orderBy`` inside
``window`` decides what order a running total accumulates in. They are
separate clauses and may disagree -- "the ten busiest days, each carrying its
running total in date order" needs them to.

**``orderBy`` is mandatory.** A running total over an unordered set is not a
loose definition of a running total, it is no definition at all: Postgres
would pick an order and return numbers that move between runs. Refusing is
the only honest answer, so :data:`WindowSpec` requires it too, for a plan
built directly in code.

**``partitionBy`` may only name dimensions the query grouped on.** A
dimension the query did not group by has no column in the grouped result to
partition on, and the alternative -- quietly widening the ``GROUP BY`` to
make one -- would change every other number in the response.

The key and metric vocabularies are the ones ``ordering.py`` already
publishes: ``partitionBy`` and ``window.orderBy``'s ``key`` take the entity's
own ``*GroupByInput`` (naming a dimension the same way ``groupBy`` does), and
``metric`` takes its ``*OrderableMetric`` enum. They answer the same two
questions this module needs answered, and a second copy of either is a second
thing to drift.
"""

from collections.abc import Mapping, Sequence
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
from public_api.aggregations.errors import (
    InvalidPlanError,
    WindowFrameError,
    WindowOrderByRequiredError,
    WindowOrderKeyNotGroupedError,
    WindowOrderVariantError,
    WindowPartitionNotGroupedError,
)
from public_api.aggregations.ordering import (
    AggregateOrderDirection,
    AppointmentTypeOrderableMetric,
    AvailableTimeOrderableMetric,
    BlockedTimeOrderableMetric,
    CalendarEventOrderableMetric,
    CalendarOrderableMetric,
    CalendarPoolOrderableMetric,
    resolve_metric_reference,
)
from public_api.aggregations.plan import (
    AggregatableEntity,
    DimensionSpec,
    MetricSpec,
    OrderSpec,
    WindowBound,
    WindowFrameSpec,
    WindowFrameType,
    WindowFunctionKind,
    WindowSpec,
    window_alias,
)
from public_api.aggregations.plan import OrderDirection as PlanOrderDirection


# ---------------------------------------------------------------------------
# Frame input
# ---------------------------------------------------------------------------
#
# The two enums are the plan's own, published to the schema here rather than
# restated as a second pair: ``strawberry.enum`` attaches a GraphQL definition
# to the class it is handed, so there is one definition of what a window bound
# is and nothing to keep in step.

WindowFrameTypeEnum = strawberry.enum(
    WindowFrameType,
    description=(
        "Frame unit. ROWS counts rows; RANGE counts peers -- every row sharing "
        "the window's ordering value counts as one step."
    ),
)

WindowBoundEnum = strawberry.enum(WindowBound, description="One end of a window frame.")


@strawberry.input(
    description=(
        "The rows 'movingAverage' reads for each row of the result, e.g. "
        "{start: PRECEDING, startOffset: 2, end: CURRENT_ROW} for a three-row "
        "average. It shapes that one function: 'runningTotal' is always "
        "cumulative from the start of the partition, and 'rank' / "
        "'percentOfTotal' have no frame -- so one query can carry a true "
        "running total and a framed moving average at the same time. "
        "'startOffset' / 'endOffset' are required by PRECEDING and FOLLOWING, "
        "and refused by the other bounds. RANGE frames take no offset."
    )
)
class WindowFrameInput:
    frame_type: WindowFrameType = WindowFrameType.ROWS
    start: WindowBound = WindowBound.UNBOUNDED_PRECEDING
    start_offset: int | None = None
    end: WindowBound = WindowBound.CURRENT_ROW
    end_offset: int | None = None


# ---------------------------------------------------------------------------
# Per-entity window ordering inputs
# ---------------------------------------------------------------------------

_WINDOW_ORDER_DESCRIPTION = (
    "One ordering term for the window itself -- what a running total "
    "accumulates along, which is independent of the field's own orderBy. Set "
    "exactly one of 'key' or 'metric'."
)


@strawberry.input(description=_WINDOW_ORDER_DESCRIPTION)
class CalendarEventWindowOrderInput:
    key: CalendarEventGroupByInput | None = None
    metric: CalendarEventOrderableMetric | None = None
    direction: AggregateOrderDirection = AggregateOrderDirection.ASC


@strawberry.input(description=_WINDOW_ORDER_DESCRIPTION)
class AvailableTimeWindowOrderInput:
    key: AvailableTimeGroupByInput | None = None
    metric: AvailableTimeOrderableMetric | None = None
    direction: AggregateOrderDirection = AggregateOrderDirection.ASC


@strawberry.input(description=_WINDOW_ORDER_DESCRIPTION)
class BlockedTimeWindowOrderInput:
    key: BlockedTimeGroupByInput | None = None
    metric: BlockedTimeOrderableMetric | None = None
    direction: AggregateOrderDirection = AggregateOrderDirection.ASC


@strawberry.input(description=_WINDOW_ORDER_DESCRIPTION)
class AppointmentTypeWindowOrderInput:
    key: AppointmentTypeGroupByInput | None = None
    metric: AppointmentTypeOrderableMetric | None = None
    direction: AggregateOrderDirection = AggregateOrderDirection.ASC


@strawberry.input(description=_WINDOW_ORDER_DESCRIPTION)
class CalendarWindowOrderInput:
    key: CalendarGroupByInput | None = None
    metric: CalendarOrderableMetric | None = None
    direction: AggregateOrderDirection = AggregateOrderDirection.ASC


@strawberry.input(description=_WINDOW_ORDER_DESCRIPTION)
class CalendarPoolWindowOrderInput:
    key: CalendarPoolGroupByInput | None = None
    metric: CalendarPoolOrderableMetric | None = None
    direction: AggregateOrderDirection = AggregateOrderDirection.ASC


# ---------------------------------------------------------------------------
# Per-entity window inputs
# ---------------------------------------------------------------------------

_WINDOW_DESCRIPTION = (
    "Running totals, moving averages, rank and share-of-partition over the "
    "grouped rows, computed as SQL window functions. 'metric' names the one "
    "column every function reads. 'partitionBy' may only name dimensions this "
    "query groups by; omitting it makes the whole result one partition. "
    "'orderBy' is required."
)


@strawberry.input(description=_WINDOW_DESCRIPTION)
class CalendarEventWindowInput:
    metric: CalendarEventOrderableMetric = CalendarEventOrderableMetric.COUNT
    partition_by: list[CalendarEventGroupByInput] | None = None
    order_by: list[CalendarEventWindowOrderInput] | None = None
    frame: WindowFrameInput | None = None


@strawberry.input(description=_WINDOW_DESCRIPTION)
class AvailableTimeWindowInput:
    metric: AvailableTimeOrderableMetric = AvailableTimeOrderableMetric.COUNT
    partition_by: list[AvailableTimeGroupByInput] | None = None
    order_by: list[AvailableTimeWindowOrderInput] | None = None
    frame: WindowFrameInput | None = None


@strawberry.input(description=_WINDOW_DESCRIPTION)
class BlockedTimeWindowInput:
    metric: BlockedTimeOrderableMetric = BlockedTimeOrderableMetric.COUNT
    partition_by: list[BlockedTimeGroupByInput] | None = None
    order_by: list[BlockedTimeWindowOrderInput] | None = None
    frame: WindowFrameInput | None = None


@strawberry.input(description=_WINDOW_DESCRIPTION)
class AppointmentTypeWindowInput:
    metric: AppointmentTypeOrderableMetric = AppointmentTypeOrderableMetric.COUNT
    partition_by: list[AppointmentTypeGroupByInput] | None = None
    order_by: list[AppointmentTypeWindowOrderInput] | None = None
    frame: WindowFrameInput | None = None


@strawberry.input(description=_WINDOW_DESCRIPTION)
class CalendarWindowInput:
    metric: CalendarOrderableMetric = CalendarOrderableMetric.COUNT
    partition_by: list[CalendarGroupByInput] | None = None
    order_by: list[CalendarWindowOrderInput] | None = None
    frame: WindowFrameInput | None = None


@strawberry.input(description=_WINDOW_DESCRIPTION)
class CalendarPoolWindowInput:
    metric: CalendarPoolOrderableMetric = CalendarPoolOrderableMetric.COUNT
    partition_by: list[CalendarPoolGroupByInput] | None = None
    order_by: list[CalendarPoolWindowOrderInput] | None = None
    frame: WindowFrameInput | None = None


# ---------------------------------------------------------------------------
# Per-entity window output types
# ---------------------------------------------------------------------------
#
# One type per entity rather than one shared type, for the same reason every
# other output type in this package is per-entity: the row types name them,
# and a shared ``WindowMetrics`` on six rows would be the one place a future
# per-entity difference could not be expressed.

_WINDOW_METRICS_DESCRIPTION = (
    "Window functions over one grouped row's metric. 'runningTotal' "
    "accumulates from the start of the partition; 'movingAverage' is the one "
    "function the frame shapes; 'rank' is a position in the window's "
    "ordering; 'percentOfTotal' is a share of the whole partition, so it sums "
    "to 100 across it. A function this query's selection did not ask for is "
    "null."
)


@strawberry.type(description=_WINDOW_METRICS_DESCRIPTION)
class CalendarEventWindowMetrics:
    running_total: float | None = None
    moving_average: float | None = None
    rank: int | None = None
    percent_of_total: float | None = None


@strawberry.type(description=_WINDOW_METRICS_DESCRIPTION)
class AvailableTimeWindowMetrics:
    running_total: float | None = None
    moving_average: float | None = None
    rank: int | None = None
    percent_of_total: float | None = None


@strawberry.type(description=_WINDOW_METRICS_DESCRIPTION)
class BlockedTimeWindowMetrics:
    running_total: float | None = None
    moving_average: float | None = None
    rank: int | None = None
    percent_of_total: float | None = None


@strawberry.type(description=_WINDOW_METRICS_DESCRIPTION)
class AppointmentTypeWindowMetrics:
    running_total: float | None = None
    moving_average: float | None = None
    rank: int | None = None
    percent_of_total: float | None = None


@strawberry.type(description=_WINDOW_METRICS_DESCRIPTION)
class CalendarWindowMetrics:
    running_total: float | None = None
    moving_average: float | None = None
    rank: int | None = None
    percent_of_total: float | None = None


@strawberry.type(description=_WINDOW_METRICS_DESCRIPTION)
class CalendarPoolWindowMetrics:
    running_total: float | None = None
    moving_average: float | None = None
    rank: int | None = None
    percent_of_total: float | None = None


# ---------------------------------------------------------------------------
# Per-entity lookup tables
# ---------------------------------------------------------------------------

WINDOW_INPUT_BY_ENTITY: Mapping[AggregatableEntity, type] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: CalendarEventWindowInput,
        AggregatableEntity.AVAILABLE_TIME: AvailableTimeWindowInput,
        AggregatableEntity.BLOCKED_TIME: BlockedTimeWindowInput,
        AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeWindowInput,
        AggregatableEntity.CALENDAR: CalendarWindowInput,
        AggregatableEntity.CALENDAR_POOL: CalendarPoolWindowInput,
    }
)

WINDOW_METRICS_TYPE_BY_ENTITY: Mapping[AggregatableEntity, type] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: CalendarEventWindowMetrics,
        AggregatableEntity.AVAILABLE_TIME: AvailableTimeWindowMetrics,
        AggregatableEntity.BLOCKED_TIME: BlockedTimeWindowMetrics,
        AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeWindowMetrics,
        AggregatableEntity.CALENDAR: CalendarWindowMetrics,
        AggregatableEntity.CALENDAR_POOL: CalendarPoolWindowMetrics,
    }
)

#: GraphQL sub-field of a ``*WindowMetrics`` to the function that computes it.
#: The names are the camelCase ones, because that is what a selection set
#: carries.
KIND_BY_SELECTION: Mapping[str, WindowFunctionKind] = MappingProxyType(
    {
        "runningTotal": WindowFunctionKind.RUNNING_TOTAL,
        "movingAverage": WindowFunctionKind.MOVING_AVERAGE,
        "rank": WindowFunctionKind.RANK,
        "percentOfTotal": WindowFunctionKind.PERCENT_OF_TOTAL,
    }
)

#: The ``*WindowMetrics`` attribute each function's value is read back into.
ATTRIBUTE_BY_KIND: Mapping[WindowFunctionKind, str] = MappingProxyType(
    {
        WindowFunctionKind.RUNNING_TOTAL: "running_total",
        WindowFunctionKind.MOVING_AVERAGE: "moving_average",
        WindowFunctionKind.RANK: "rank",
        WindowFunctionKind.PERCENT_OF_TOTAL: "percent_of_total",
    }
)

_DIRECTION_BY_GRAPHQL: Mapping[AggregateOrderDirection, PlanOrderDirection] = MappingProxyType(
    {
        AggregateOrderDirection.ASC: PlanOrderDirection.ASC,
        AggregateOrderDirection.DESC: PlanOrderDirection.DESC,
    }
)


# ---------------------------------------------------------------------------
# Input to plan
# ---------------------------------------------------------------------------


class WindowOrderEntry(Protocol):
    """The shape every entity's window-ordering input has.

    Structural, like ``ordering.OrderEntry``: each entity's slots are typed
    to its own inputs and enums, so there is nothing to inherit but this
    contract.
    """

    key: Any
    metric: Any
    direction: AggregateOrderDirection


class WindowEntry(Protocol):
    """The shape every entity's ``window`` argument has."""

    metric: Any
    partition_by: Any
    order_by: Any
    frame: WindowFrameInput | None


def resolve_frame(frame: WindowFrameInput | None) -> WindowFrameSpec | None:
    """A frame input as a plan spec, refusing one Postgres would reject.

    ``None`` stays ``None`` and leaves the executor on its default -- the
    whole partition up to the current row, which is what a running total
    means.

    :class:`~public_api.aggregations.plan.WindowFrameSpec` validates itself on
    construction, so a bad frame is refused here, while the plan is still
    being built and before a database connection is taken. Its refusal is an
    engine error, raised for a plan built directly in code; a frame that
    arrived in a GraphQL document is a caller's mistake, so the same wording
    travels back out as a :class:`WindowFrameError` the resolver may let
    through.
    """
    if frame is None:
        return None
    try:
        return WindowFrameSpec(
            frame_type=frame.frame_type,
            start=frame.start,
            start_offset=frame.start_offset,
            end=frame.end,
            end_offset=frame.end_offset,
        )
    except InvalidPlanError as exc:
        raise WindowFrameError(str(exc)) from None


def _grouped_alias(
    entry: Any,
    timezone_name: str,
    dimensions: Sequence[DimensionSpec],
    error: type[Exception],
) -> str:
    """The dimension alias one ``*GroupByInput`` names, if the query grouped by it.

    Resolved through the same
    :func:`~public_api.aggregations.dimensions.resolve_dimensions` ``groupBy``
    itself goes through, so a caller naming the dimension it actually grouped
    by always lands on a matching alias -- including a temporal one, which
    lives under ``<field>_bucket`` rather than under its column name.

    The whole resolved :class:`DimensionSpec` is compared, not the alias
    alone, and that is the load-bearing part.
    ``public_api.aggregations.dimensions.temporal_alias`` does not encode the
    granularity, so ``START_TIME/DAY`` and ``START_TIME/MONTH`` both resolve
    to ``start_time_bucket``. Matching on the alias would accept a window
    partitioned by month over a result grouped by day: the partition would
    silently be the day bucket, every partition would hold one row, and the
    running total would equal the row's own count on every row while the
    share of partition read 100 everywhere -- confident numbers, no error.
    """
    resolved = resolve_dimensions([entry], timezone_name)[0]
    if resolved not in dimensions:
        raise error()
    return resolved.alias


def resolve_window(
    entity: AggregatableEntity,
    window_input: WindowEntry | None,
    timezone_name: str,
    dimensions: Sequence[DimensionSpec],
    selected_kinds: Sequence[WindowFunctionKind],
) -> tuple[WindowSpec | None, tuple[MetricSpec, ...]]:
    """A ``window`` argument, as a plan spec plus the metrics it needs annotated.

    Returns ``(None, ())`` for an absent argument, which is what keeps a query
    without ``window`` identical to one from before this phase existed.

    ``dimensions`` is this query's own resolved ``groupBy``, whole rather than
    as a list of aliases: see :func:`_grouped_alias` for why the granularity
    has to be part of the comparison.

    Like ``having.py`` and ``ordering.py``, a window may read a metric the row
    selection never asked to see; it is folded into the returned metrics so
    the executor annotates it like any other.

    ``selected_kinds`` is what the document's ``window { ... }`` sub-selection
    asked for. A window whose sub-selection named nothing still validates --
    a bad ``partitionBy`` is a bad query whether or not its result would be
    read -- but annotates no window column.
    """
    if window_input is None:
        return None, ()

    # Before anything else: the plan would refuse an unordered window too, but
    # with an engine-internal ``InvalidPlanError``. Reached through a real
    # query it is a caller's mistake, so it is refused here with a message the
    # caller may read.
    if not window_input.order_by:
        raise WindowOrderByRequiredError()

    metric = resolve_metric_reference(entity, window_input.metric)
    required: dict[str, MetricSpec] = {metric.alias: metric}

    order_specs: list[OrderSpec] = []
    for entry in window_input.order_by:
        if (entry.key is None) == (entry.metric is None):
            raise WindowOrderVariantError()
        direction = _DIRECTION_BY_GRAPHQL[entry.direction]
        if entry.key is not None:
            alias = _grouped_alias(
                entry.key, timezone_name, dimensions, WindowOrderKeyNotGroupedError
            )
            order_specs.append(OrderSpec(alias=alias, direction=direction))
            continue
        ordered_metric = resolve_metric_reference(entity, entry.metric)
        required.setdefault(ordered_metric.alias, ordered_metric)
        order_specs.append(OrderSpec(alias=ordered_metric.alias, direction=direction))

    partition_aliases: list[str] = []
    for entry in window_input.partition_by or ():
        alias = _grouped_alias(entry, timezone_name, dimensions, WindowPartitionNotGroupedError)
        # Naming the same dimension twice is the same partition.
        if alias not in partition_aliases:
            partition_aliases.append(alias)

    # Ordered by ``WindowFunctionKind`` rather than by the order the document
    # happened to select them in, so two documents asking for the same
    # functions build the same plan and audit the same way.
    selected = set(selected_kinds)
    functions = tuple(kind for kind in WindowFunctionKind if kind in selected)

    spec = WindowSpec(
        metric_alias=metric.alias,
        order_by=tuple(order_specs),
        functions=functions,
        partition_by=tuple(partition_aliases),
        frame=resolve_frame(window_input.frame),
    )
    return spec, tuple(required.values())


def build_window_metrics(
    entity: AggregatableEntity, row: Mapping[str, Any], spec: WindowSpec
) -> Any:
    """One result row's window columns, as the entity's ``*WindowMetrics``.

    A function the plan did not compute is absent from the row and keeps the
    type's ``None`` default -- the honest "not asked for".
    """
    metrics_type = WINDOW_METRICS_TYPE_BY_ENTITY[entity]
    return metrics_type(
        **{ATTRIBUTE_BY_KIND[kind]: row.get(window_alias(kind)) for kind in spec.functions}
    )


def selected_window_kinds(selection_names: Sequence[str]) -> tuple[WindowFunctionKind, ...]:
    """The window functions a ``window { ... }`` sub-selection asked for.

    Names that are not window functions -- ``__typename``, say -- are ignored
    rather than refused: the schema already decided what may appear there.
    """
    selected = {KIND_BY_SELECTION[name] for name in selection_names if name in KIND_BY_SELECTION}
    return tuple(kind for kind in WindowFunctionKind if kind in selected)
