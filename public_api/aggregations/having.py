"""Typed post-aggregation filtering: HAVING, per entity.

Scoped deliberately narrower than the full metric surface: a caller may
filter on the row count, on a relation count, or on a numeric metric's
``sum`` / ``avg`` / ``min`` / ``max`` -- the values a "busiest calendars"
style question actually compares. String, datetime and boolean aggregates
are not HAVING-able in this phase; there is no ``StringAggregateComparison``
or its kin, and adding one is future scope, not an oversight.

**Semantics are AND-by-default, the way most GraphQL filter APIs read.**
Every field a ``*HavingInput`` node sets -- ``count``, a relation count, a
numeric metric, ``and``, ``or`` -- combines with every other one it also
sets, via AND. Setting only ``or`` filters by the OR of its children; nesting
``and`` inside ``or`` (or the reverse) composes the usual way. A node or a
comparison that sets nothing at all is refused rather than silently matching
every group -- see :class:`EmptyHavingInputError` /
:class:`EmptyHavingComparisonError`.

**Resolution is reflection-based, not per-entity.** Every ``*HavingInput``
follows one convention: an attribute's name is the registry field path it
filters (``duration_minutes``, ``attendance_count``, ...), and its value's
*type* -- :class:`IntComparison` or :class:`NumericAggregateComparison` --
says whether it names the row count / a relation count or a numeric metric.
:func:`resolve_having` reads that convention off whichever input a caller's
entity uses, so one function serves all six rather than six near-identical
copies that could drift from each other.
"""

import dataclasses
from types import MappingProxyType
from typing import Any

import strawberry

from public_api.aggregations.errors import EmptyHavingComparisonError, EmptyHavingInputError
from public_api.aggregations.plan import (
    ROW_COUNT_ALIAS,
    ROW_COUNT_FIELD_PATH,
    AggregatableEntity,
    AggregateOp,
    ComparisonOp,
    HavingComparison,
    HavingSpec,
    MetricSpec,
    default_metric_alias,
)


@strawberry.input(
    description=(
        "Integer comparison. Every operator that is set applies, combined with AND; "
        "at least one must be set."
    )
)
class IntComparison:
    eq: int | None = None
    ne: int | None = None
    gt: int | None = None
    gte: int | None = None
    lt: int | None = None
    lte: int | None = None


@strawberry.input(
    description=(
        "Float comparison. Every operator that is set applies, combined with AND; "
        "at least one must be set."
    )
)
class FloatComparison:
    eq: float | None = None
    ne: float | None = None
    gt: float | None = None
    gte: float | None = None
    lt: float | None = None
    lte: float | None = None


@strawberry.input(
    description=(
        "Comparisons over a numeric field's aggregate operations. Set whichever "
        "operations the comparison is over; each applies independently, combined "
        "with AND."
    )
)
class NumericAggregateComparison:
    sum: FloatComparison | None = None
    avg: FloatComparison | None = None
    min: FloatComparison | None = None
    max: FloatComparison | None = None


#: GraphQL field name to the plan's comparison enum member -- shared by every
#: ``IntComparison`` / ``FloatComparison`` this module resolves.
_COMPARISON_FIELDS: tuple[tuple[str, ComparisonOp], ...] = (
    ("eq", ComparisonOp.EQ),
    ("ne", ComparisonOp.NE),
    ("gt", ComparisonOp.GT),
    ("gte", ComparisonOp.GTE),
    ("lt", ComparisonOp.LT),
    ("lte", ComparisonOp.LTE),
)

#: ``NumericAggregateComparison``'s own field names, paired with the
#: aggregate operation each names -- the numeric counterpart of
#: ``_COMPARISON_FIELDS`` above.
_NUMERIC_AGGREGATE_FIELDS: tuple[tuple[str, AggregateOp], ...] = (
    ("sum", AggregateOp.SUM),
    ("avg", AggregateOp.AVG),
    ("min", AggregateOp.MIN),
    ("max", AggregateOp.MAX),
)


def _leaf_comparisons(
    alias: str, comparison: IntComparison | FloatComparison
) -> tuple[HavingSpec, ...]:
    """Every operator ``comparison`` set, as one leaf ``HavingSpec`` each."""
    leaves = tuple(
        HavingSpec(comparison=HavingComparison(alias=alias, comparison=op, value=value))
        for field_name, op in _COMPARISON_FIELDS
        if (value := getattr(comparison, field_name)) is not None
    )
    if not leaves:
        raise EmptyHavingComparisonError()
    return leaves


def _numeric_leaf_specs(
    field_path: str, comparison: NumericAggregateComparison
) -> tuple[tuple[HavingSpec, ...], tuple[MetricSpec, ...]]:
    """Every operation ``comparison`` set, as leaves plus the metrics they need."""
    leaves: list[HavingSpec] = []
    metrics: list[MetricSpec] = []
    for field_name, op in _NUMERIC_AGGREGATE_FIELDS:
        sub_comparison: FloatComparison | None = getattr(comparison, field_name)
        if sub_comparison is None:
            continue
        alias = default_metric_alias(field_path, op)
        metrics.append(MetricSpec(alias=alias, field_path=field_path, op=op))
        leaves.extend(_leaf_comparisons(alias, sub_comparison))
    if not leaves:
        raise EmptyHavingComparisonError()
    return tuple(leaves), tuple(metrics)


def _field_leaf_specs(
    field_name: str, value: Any
) -> tuple[tuple[HavingSpec, ...], tuple[MetricSpec, ...]]:
    """One ``*HavingInput`` attribute's leaves and the metrics it needs.

    ``field_name`` is the attribute name, which -- by the convention every
    ``*HavingInput`` follows -- is also the registry field path, except for
    ``count``, the one field that names the row count rather than a relation.
    """
    if isinstance(value, IntComparison):
        field_path = ROW_COUNT_FIELD_PATH if field_name == "count" else field_name
        alias = ROW_COUNT_ALIAS if field_name == "count" else field_name
        metric = MetricSpec(alias=alias, field_path=field_path, op=AggregateOp.COUNT)
        return _leaf_comparisons(alias, value), (metric,)
    if isinstance(value, NumericAggregateComparison):
        return _numeric_leaf_specs(field_name, value)
    raise AssertionError(f"Unhandled having field type {type(value)!r}")  # pragma: no cover


def _resolve_having_node(having_input: Any) -> tuple[HavingSpec, tuple[MetricSpec, ...]]:
    """One non-``None`` ``*HavingInput`` node, as a ``HavingSpec`` plus the
    metrics it needs. Recurses into ``and`` / ``or`` children, which are
    never ``None`` themselves -- a GraphQL list has no null entries here."""
    leaves: list[HavingSpec] = []
    metrics: list[MetricSpec] = []

    for having_field in dataclasses.fields(having_input):
        if having_field.name in ("and_", "or_"):
            continue
        value = getattr(having_input, having_field.name)
        if value is None:
            continue
        field_leaves, field_metrics = _field_leaf_specs(having_field.name, value)
        leaves.extend(field_leaves)
        metrics.extend(field_metrics)

    and_children = getattr(having_input, "and_", None) or ()
    for child_input in and_children:
        child_spec, child_metrics = _resolve_having_node(child_input)
        leaves.append(child_spec)
        metrics.extend(child_metrics)

    or_children = getattr(having_input, "or_", None) or ()
    or_leaves: list[HavingSpec] = []
    for child_input in or_children:
        child_spec, child_metrics = _resolve_having_node(child_input)
        or_leaves.append(child_spec)
        metrics.extend(child_metrics)
    if or_leaves:
        leaves.append(HavingSpec(any_of=tuple(or_leaves)) if len(or_leaves) > 1 else or_leaves[0])

    if not leaves:
        raise EmptyHavingInputError()

    spec = HavingSpec(all_of=tuple(leaves)) if len(leaves) > 1 else leaves[0]
    return spec, tuple(metrics)


def resolve_having(having_input: Any | None) -> tuple[HavingSpec | None, tuple[MetricSpec, ...]]:
    """One ``*HavingInput`` (or ``None``), as a plan-ready ``HavingSpec``.

    Also returns every :class:`MetricSpec` the clause needs -- including ones
    for a metric the row selection never asked for -- so the caller (
    ``public_api.aggregations.fields``) can fold them into the plan's
    ``metrics`` before it is built. This is what keeps a HAVING clause on an
    unrequested metric from erroring: by the time the plan exists, the metric
    it needs is already one of ``plan.metrics``, annotated exactly the way a
    row-selected one would be.
    """
    if having_input is None:
        return None, ()
    return _resolve_having_node(having_input)


# ---------------------------------------------------------------------------
# Per-entity HAVING inputs
# ---------------------------------------------------------------------------
#
# One class per entity, each naming exactly the row count, the entity's
# relation counts, and its numeric metrics -- the same restriction the
# module docstring describes. ``and`` / ``or`` are Python keywords, so the
# attribute is ``and_`` / ``or_`` and ``strawberry.field(name=...)`` renames
# it back for the schema.

_HAVING_INPUT_DESCRIPTION = (
    "Post-aggregation filter. Every field that is set applies, combined with "
    "AND; 'and' / 'or' combine nested filters explicitly."
)


@strawberry.input(description=_HAVING_INPUT_DESCRIPTION)
class CalendarEventHavingInput:
    count: IntComparison | None = None
    duration_minutes: NumericAggregateComparison | None = None
    attendance_count: IntComparison | None = None
    external_attendance_count: IntComparison | None = None
    resource_allocation_count: IntComparison | None = None
    and_: "list[CalendarEventHavingInput] | None" = strawberry.field(name="and", default=None)
    or_: "list[CalendarEventHavingInput] | None" = strawberry.field(name="or", default=None)


@strawberry.input(description=_HAVING_INPUT_DESCRIPTION)
class AvailableTimeHavingInput:
    count: IntComparison | None = None
    duration_minutes: NumericAggregateComparison | None = None
    and_: "list[AvailableTimeHavingInput] | None" = strawberry.field(name="and", default=None)
    or_: "list[AvailableTimeHavingInput] | None" = strawberry.field(name="or", default=None)


@strawberry.input(description=_HAVING_INPUT_DESCRIPTION)
class BlockedTimeHavingInput:
    count: IntComparison | None = None
    duration_minutes: NumericAggregateComparison | None = None
    and_: "list[BlockedTimeHavingInput] | None" = strawberry.field(name="and", default=None)
    or_: "list[BlockedTimeHavingInput] | None" = strawberry.field(name="or", default=None)


@strawberry.input(description=_HAVING_INPUT_DESCRIPTION)
class AppointmentTypeHavingInput:
    count: IntComparison | None = None
    duration_minutes: NumericAggregateComparison | None = None
    event_count: IntComparison | None = None
    slot_count: IntComparison | None = None
    and_: "list[AppointmentTypeHavingInput] | None" = strawberry.field(name="and", default=None)
    or_: "list[AppointmentTypeHavingInput] | None" = strawberry.field(name="or", default=None)


@strawberry.input(description=_HAVING_INPUT_DESCRIPTION)
class CalendarHavingInput:
    count: IntComparison | None = None
    capacity: NumericAggregateComparison | None = None
    event_count: IntComparison | None = None
    blocked_time_count: IntComparison | None = None
    available_time_count: IntComparison | None = None
    and_: "list[CalendarHavingInput] | None" = strawberry.field(name="and", default=None)
    or_: "list[CalendarHavingInput] | None" = strawberry.field(name="or", default=None)


@strawberry.input(description=_HAVING_INPUT_DESCRIPTION)
class CalendarPoolHavingInput:
    count: IntComparison | None = None
    calendar_count: IntComparison | None = None
    and_: "list[CalendarPoolHavingInput] | None" = strawberry.field(name="and", default=None)
    or_: "list[CalendarPoolHavingInput] | None" = strawberry.field(name="or", default=None)


#: Per-entity HAVING input, keyed the same way every other per-entity lookup
#: table in this package is.
HAVING_INPUT_BY_ENTITY: MappingProxyType[AggregatableEntity, type] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: CalendarEventHavingInput,
        AggregatableEntity.AVAILABLE_TIME: AvailableTimeHavingInput,
        AggregatableEntity.BLOCKED_TIME: BlockedTimeHavingInput,
        AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeHavingInput,
        AggregatableEntity.CALENDAR: CalendarHavingInput,
        AggregatableEntity.CALENDAR_POOL: CalendarPoolHavingInput,
    }
)
