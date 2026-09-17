"""The GraphQL surface of window functions: running totals, moving averages, rank.

A metric answers "how many on this day". A window answers "how many so far",
"how does this day compare to the fortnight around it", "where does this day
rank" -- questions about a row's place in the *sequence* of rows, which no
amount of grouping can express. Postgres computes them after `GROUP BY` and
`HAVING`, which is what makes them expressible over an already-aggregated
result at all.

**What a caller supplies.** A `partitionBy` naming group-key dimensions, an
`orderBy` saying in what sequence, and an optional `frame` saying how much of
the sequence a moving average looks at. Ordering is mandatory: a running total
over rows in no particular order is a different number on every run, with
nothing in the response to say so, so it is refused rather than defaulted.

**What comes back.** A per-entity `*WindowMetrics`, generated from the registry
the same way the row types are, with `running*` / `movingAvg*` for every
numeric metric plus `rank` and `percentOfTotal`. Selecting one is what asks for
it; a document with no `window` sub-selection computes no window functions and
emits SQL with no `OVER` clause in it.

**Which frame applies to what.** The caller's `frame` is read by `movingAvg*`
and nothing else. `running*` is cumulative by definition -- a running total
over a three-row frame is not a running total -- so it fixes its own frame at
`UNBOUNDED PRECEDING` to `CURRENT ROW`. `rank` is a position and `percentOfTotal`
is a share of the whole partition; neither takes a frame. This is stated on the
fields themselves as well as here, because a frame that silently applied to a
running total would return numbers that look right.
"""

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import strawberry
from strawberry.utils.str_converters import to_camel_case

from public_api.aggregations.errors import UngroupedPartitionKeyError
from public_api.aggregations.ordering import (
    METRIC_REF_ENUM_BY_ENTITY,
    METRIC_SPEC_BY_ALIAS_BY_ENTITY,
    ORDER_KEY_ENUM_BY_ENTITY,
    OrderDirection,
    order_from_input,
)
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    DimensionSpec,
    MetricSpec,
    WindowFunction,
    WindowMetricSpec,
    WindowSpec,
)
from public_api.aggregations.registry import (
    REGISTRY,
    AggregateKind,
    EntityRegistration,
    metric_alias,
)


# Window metric aliases live under their own prefix so they cannot collide with
# a dimension alias, a metric alias, or a column of the model being grouped.
WINDOW_ALIAS_PREFIX = "_w_"

# The row alias the group's own count lands under, shared with `having` and
# `ordering`. Every count-based window metric windows over this.
ROW_COUNT_ALIAS = "count"


@strawberry.enum(
    description=(
        "Where a frame bound sits. `PRECEDING` and `FOLLOWING` each need the "
        "matching row offset alongside them; the other three are positions and "
        "take none."
    )
)
class WindowBound(enum.Enum):
    UNBOUNDED_PRECEDING = "UNBOUNDED_PRECEDING"
    PRECEDING = "PRECEDING"
    CURRENT_ROW = "CURRENT_ROW"
    FOLLOWING = "FOLLOWING"
    UNBOUNDED_FOLLOWING = "UNBOUNDED_FOLLOWING"


@strawberry.enum(
    description=(
        "What a frame counts. Rows only: this API's buckets are sparse -- a day "
        "with no matching rows produces no row -- so a value-based `RANGE` frame "
        "over a date ordering would span a different number of buckets than the "
        "caller counted, silently."
    )
)
class WindowFrameType(enum.Enum):
    ROWS = "ROWS"


@strawberry.input(
    description=(
        "How much of the ordered sequence a moving average reads. Read by "
        "`movingAvg*` only -- `running*` is cumulative by definition, and "
        "`rank` and `percentOfTotal` take no frame."
    )
)
class WindowFrameInput:
    type: WindowFrameType = WindowFrameType.ROWS
    start: WindowBound = WindowBound.UNBOUNDED_PRECEDING
    end: WindowBound = WindowBound.CURRENT_ROW
    start_offset: int | None = strawberry.field(
        default=None, description="How many rows back, when `start` is PRECEDING or FOLLOWING."
    )
    end_offset: int | None = strawberry.field(
        default=None, description="How many rows on, when `end` is PRECEDING or FOLLOWING."
    )


def _entity_class_prefix(entity: AggregatableEntity) -> str:
    """`calendar_event` -> `CalendarEvent`."""
    return "".join(part.title() for part in entity.value.split("_"))


# ---------------------------------------------------------------------------
# Which window metrics an entity offers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _WindowMetricDefinition:
    """One field on a `*WindowMetrics` type, and what it compiles to.

    `name` is the Python attribute (Strawberry camel-cases it into the schema);
    `alias` is the row key the same value lands under, kept apart by
    :data:`WINDOW_ALIAS_PREFIX` so it can never be mistaken for a metric or a
    dimension.
    """

    name: str
    function: WindowFunction
    source_alias: str
    output: type
    description: str

    @property
    def alias(self) -> str:
        """The row key this window metric lands under."""
        return f"{WINDOW_ALIAS_PREFIX}{self.name}"


def _window_metric_definitions(
    registration: EntityRegistration,
) -> tuple[_WindowMetricDefinition, ...]:
    """Every window metric this entity offers, derived from its registration.

    The count-based four are on every entity; the numeric ones follow whatever
    the registry says is summable, so a numeric field added there gains a
    running total and a moving average without a second edit here.
    """
    definitions: list[_WindowMetricDefinition] = [
        _WindowMetricDefinition(
            "running_count",
            WindowFunction.RUNNING_SUM,
            ROW_COUNT_ALIAS,
            int,
            "Rows counted from the start of the partition up to and including this one.",
        ),
        _WindowMetricDefinition(
            "moving_avg_count",
            WindowFunction.MOVING_AVG,
            ROW_COUNT_ALIAS,
            float,
            "Average row count over the frame. Reads the window's `frame`.",
        ),
        _WindowMetricDefinition(
            "rank",
            WindowFunction.RANK,
            "",
            int,
            "This row's position in the window's ordering. Ties share a rank.",
        ),
        _WindowMetricDefinition(
            "percent_of_total",
            WindowFunction.PERCENT_OF_TOTAL,
            ROW_COUNT_ALIAS,
            float,
            "This row's count as a percentage of its whole partition's count.",
        ),
    ]

    for spec in registration.aggregatable:
        if spec.kind is not AggregateKind.NUMERIC:
            continue
        source = metric_alias(spec.name, AggregateOp.SUM)
        definitions.append(
            _WindowMetricDefinition(
                f"running_{spec.name}",
                WindowFunction.RUNNING_SUM,
                source,
                float,
                f"`{to_camel_case(spec.name)}` summed from the start of the partition "
                f"up to and including this row.",
            )
        )
        definitions.append(
            _WindowMetricDefinition(
                f"moving_avg_{spec.name}",
                WindowFunction.MOVING_AVG,
                source,
                float,
                f"Average of `{to_camel_case(spec.name)}`'s per-row sum over the frame. "
                f"Reads the window's `frame`.",
            )
        )
    return tuple(definitions)


WINDOW_METRIC_DEFINITIONS_BY_ENTITY: Mapping[
    AggregatableEntity, tuple[_WindowMetricDefinition, ...]
] = {entity: _window_metric_definitions(registration) for entity, registration in REGISTRY.items()}


# ---------------------------------------------------------------------------
# Generated per-entity types
# ---------------------------------------------------------------------------


def _build_window_metrics_type(entity: AggregatableEntity) -> type:
    """One entity's `*WindowMetrics` output type."""
    prefix = _entity_class_prefix(entity)
    annotations: dict[str, Any] = {}
    namespace: dict[str, Any] = {}
    for definition in WINDOW_METRIC_DEFINITIONS_BY_ENTITY[entity]:
        annotations[definition.name] = definition.output | None
        namespace[definition.name] = strawberry.field(
            default=None, description=definition.description
        )
    namespace["__annotations__"] = annotations
    namespace["__module__"] = __name__
    namespace["__doc__"] = f"Window functions over a {prefix} aggregate's ordered rows."
    metrics_class = type(f"{prefix}WindowMetrics", (), namespace)
    return strawberry.type(
        metrics_class,
        description=(
            f"Values computed over this row's place in the ordered sequence of "
            f"{prefix} groups, rather than over the rows inside the group."
        ),
    )


def _build_window_order_input(entity: AggregatableEntity) -> type:
    """One entity's `*WindowOrderInput`.

    Deliberately the same shape as the result ordering input: the sequence a
    running total accumulates in is described the same way as the sequence rows
    come back in, and it is resolved by the same function.
    """
    prefix = _entity_class_prefix(entity)
    annotations: dict[str, Any] = {
        "key": ORDER_KEY_ENUM_BY_ENTITY[entity] | None,
        "metric": METRIC_REF_ENUM_BY_ENTITY[entity] | None,
        "direction": OrderDirection,
    }
    namespace: dict[str, Any] = {
        "key": strawberry.field(default=None, description="Sequence by a group-key dimension."),
        "metric": strawberry.field(default=None, description="Sequence by an aggregated value."),
        "direction": strawberry.field(
            default=OrderDirection.ASC,
            description="Sequence direction. Ascending by default: a running total normally "
            "accumulates forwards.",
        ),
        "__annotations__": annotations,
        "__module__": __name__,
        "__doc__": f"One term of a {prefix} window's ordering.",
    }
    order_class = type(f"{prefix}WindowOrderInput", (), namespace)
    return strawberry.input(
        order_class,
        description=(
            f"How to sequence {prefix} rows inside a window. Name exactly one of `key` or `metric`."
        ),
    )


WINDOW_METRICS_TYPE_BY_ENTITY: Mapping[AggregatableEntity, type] = {
    entity: _build_window_metrics_type(entity) for entity in REGISTRY
}

WINDOW_ORDER_INPUT_TYPE_BY_ENTITY: Mapping[AggregatableEntity, type] = {
    entity: _build_window_order_input(entity) for entity in REGISTRY
}


def _build_window_input(entity: AggregatableEntity) -> type:
    """One entity's `*WindowInput`."""
    prefix = _entity_class_prefix(entity)
    order_type = WINDOW_ORDER_INPUT_TYPE_BY_ENTITY[entity]
    key_enum = ORDER_KEY_ENUM_BY_ENTITY[entity]
    annotations: dict[str, Any] = {
        "order_by": list[order_type],  # type: ignore[valid-type]
        "partition_by": list[key_enum] | None,  # type: ignore[valid-type]
        "frame": WindowFrameInput | None,
    }
    namespace: dict[str, Any] = {
        "order_by": strawberry.field(
            description=(
                "Required. A running total over rows in no particular order has no "
                "defined value, so there is no ordering this API could pick for you."
            )
        ),
        "partition_by": strawberry.field(
            default=None,
            description=(
                "Restart the window for each distinct value. May name only "
                "dimensions this query also grouped by."
            ),
        ),
        "frame": strawberry.field(
            default=None, description="How much of the sequence `movingAvg*` reads."
        ),
        "__annotations__": annotations,
        "__module__": __name__,
        "__doc__": f"A window over {prefix} aggregate rows.",
    }
    window_class = type(f"{prefix}WindowInput", (), namespace)
    return strawberry.input(
        window_class,
        description=(
            f"Compute values over this row's place in the ordered sequence of "
            f"{prefix} groups. `orderBy` is required."
        ),
    )


WINDOW_INPUT_TYPE_BY_ENTITY: Mapping[AggregatableEntity, type] = {
    entity: _build_window_input(entity) for entity in REGISTRY
}


# ---------------------------------------------------------------------------
# Input -> plan
# ---------------------------------------------------------------------------


def _partition_alias(dimensions: Sequence[DimensionSpec], field_name: str) -> str:
    """The row key a partition dimension landed under in *this* query.

    Read off the plan's own dimensions rather than recomputed, because a
    bucketed dimension's alias carries its granularity. A dimension the query
    did not group by has no alias to find, and partitioning on it would widen
    the `GROUP BY` rather than split the rows.
    """
    for dimension in dimensions:
        if dimension.field_path == field_name:
            return dimension.alias
    raise UngroupedPartitionKeyError(field_name)


def _frame_arguments(frame: Any) -> dict[str, Any]:
    """The frame half of a `WindowSpec`, defaulted the way the schema defaults it."""
    if frame is None:
        return {}
    return {
        "frame_type": frame.type.value,
        "frame_start": frame.start.value,
        "frame_end": frame.end.value,
        "frame_start_offset": frame.start_offset,
        "frame_end_offset": frame.end_offset,
    }


def window_from_input(
    entity: AggregatableEntity,
    window_input: Any,
    selected_metric_names: Sequence[str],
    dimensions: Sequence[DimensionSpec],
) -> tuple[WindowSpec | None, tuple[MetricSpec, ...]]:
    """Turn a field's `window` argument plus its sub-selection into a `WindowSpec`.

    Returns the metrics the window reads alongside it, for the same reason
    `having_from_input` and `order_from_input` do: a running total over a
    metric the document did not select still has to annotate that metric, since
    the `OVER` clause wraps the aggregate expression rather than the row key.

    `selected_metric_names` is which fields of the `*WindowMetrics` type the
    document actually asked for. A `window` argument with no sub-selection
    computes nothing -- there is no window function to run.
    """
    if window_input is None:
        return None, ()

    definitions = {
        definition.name: definition for definition in WINDOW_METRIC_DEFINITIONS_BY_ENTITY[entity]
    }
    requested = [definitions[name] for name in selected_metric_names if name in definitions]
    if not requested:
        return None, ()

    # Resolved by the result-ordering converter: a window's sequence is
    # described exactly the way a result's ordering is, and having one function
    # decide what an alias means keeps the two from disagreeing.
    order_specs, order_metrics = order_from_input(
        entity, list(window_input.order_by or ()), dimensions
    )

    partition_by = tuple(
        _partition_alias(dimensions, key.value) for key in (window_input.partition_by or ())
    )

    metrics: list[WindowMetricSpec] = []
    required: dict[str, MetricSpec] = {spec.alias: spec for spec in order_metrics}
    for definition in requested:
        metrics.append(
            WindowMetricSpec(
                alias=definition.alias,
                function=definition.function,
                source_alias=definition.source_alias,
            )
        )
        if definition.source_alias:
            required.setdefault(definition.source_alias, _source_metric(entity, definition))

    spec = WindowSpec(
        order_by=order_specs,
        partition_by=partition_by,
        metrics=tuple(metrics),
        **_frame_arguments(window_input.frame),
    )
    return spec, tuple(required.values())


def _source_metric(entity: AggregatableEntity, definition: _WindowMetricDefinition) -> MetricSpec:
    """The grouped metric a window metric windows over.

    Looked up through the ordering module's alias table, which is built from
    the same registry, so a window and an `orderBy` naming the same metric
    resolve to one annotation rather than two.
    """
    return METRIC_SPEC_BY_ALIAS_BY_ENTITY[entity][definition.source_alias]


def build_window_metrics(
    entity: AggregatableEntity,
    selected_metric_names: Sequence[str],
    row: Mapping[str, Any],
) -> Any:
    """Populate `entity`'s `*WindowMetrics` from one aggregated row.

    A field-by-field copy out of the row dict, like the group key: the database
    computed every value here.
    """
    metrics_class = WINDOW_METRICS_TYPE_BY_ENTITY[entity]
    definitions = {
        definition.name: definition for definition in WINDOW_METRIC_DEFINITIONS_BY_ENTITY[entity]
    }
    values: dict[str, Any] = {}
    for name in selected_metric_names:
        definition = definitions.get(name)
        if definition is None or definition.alias not in row:
            continue
        raw = row[definition.alias]
        values[name] = None if raw is None else definition.output(raw)
    return metrics_class(**values)


def window_metrics_type(entity: AggregatableEntity) -> type:
    """The `*WindowMetrics` output type for `entity`."""
    return WINDOW_METRICS_TYPE_BY_ENTITY[entity]


def window_input_type(entity: AggregatableEntity) -> type:
    """The `*WindowInput` for `entity`."""
    return WINDOW_INPUT_TYPE_BY_ENTITY[entity]


__all__ = [
    "WINDOW_ALIAS_PREFIX",
    "WINDOW_INPUT_TYPE_BY_ENTITY",
    "WINDOW_METRICS_TYPE_BY_ENTITY",
    "WINDOW_METRIC_DEFINITIONS_BY_ENTITY",
    "WINDOW_ORDER_INPUT_TYPE_BY_ENTITY",
    "WindowBound",
    "WindowFrameInput",
    "WindowFrameType",
    "build_window_metrics",
    "window_from_input",
    "window_input_type",
    "window_metrics_type",
]
