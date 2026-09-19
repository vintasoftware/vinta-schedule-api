"""Filtering groups on what they aggregate to — the ``HAVING`` half of top-N.

``WHERE`` narrows the rows that go into a group; ``HAVING`` narrows the groups
that come out. The difference is the whole point of this module: a filter input
(Phase 1) drops events before they are counted, and a having input drops
calendars whose count came out too small. Both end up in the same statement and
neither is computed in Python.

The comparisons compare numbers, so the referenceable metrics are the numbers:
the group's own ``count``, the four operations over each numeric field, and each
relation count. ``registry.referenceable_metrics`` is the one place that decides
that set, and ``ordering.py`` reads the same one — a metric you can order by is
a metric you can filter on.

A clause may name a metric the document did not select. The engine annotates it
anyway and simply does not read it back, because "calendars with more than
twenty events" is a perfectly good question to ask without also displaying the
number.
"""

from collections.abc import Mapping, Sequence

import strawberry

from public_api.aggregations.errors import InvalidAggregatePlanError
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    ComparisonOperator,
    HavingCondition,
    HavingSpec,
    MetricSpec,
)
from public_api.aggregations.registry import (
    COUNT_METRIC_NAME,
    FieldKind,
    MetricReference,
    get_registration,
    referenceable_metrics,
)


#: Comparison input field to the operator it becomes. The names are the GraphQL
#: ones; ``eq`` is last so a reader sees the ordering comparisons together.
OPERATOR_FOR_COMPARISON: Mapping[str, ComparisonOperator] = {
    "gt": ComparisonOperator.GT,
    "gte": ComparisonOperator.GTE,
    "lt": ComparisonOperator.LT,
    "lte": ComparisonOperator.LTE,
    "eq": ComparisonOperator.EQ,
}

#: ``NumericAggregateComparison`` field to the operation it constrains.
OP_FOR_NUMERIC_COMPARISON: Mapping[str, AggregateOp] = {
    "sum": AggregateOp.SUM,
    "avg": AggregateOp.AVG,
    "min": AggregateOp.MIN,
    "max": AggregateOp.MAX,
}


# ---------------------------------------------------------------------------
# Comparison inputs
# ---------------------------------------------------------------------------


@strawberry.input(description="Compare an integer metric. Every set field is ANDed.")
class IntComparison:
    eq: int | None = None
    gt: int | None = None
    gte: int | None = None
    lt: int | None = None
    lte: int | None = None


@strawberry.input(description="Compare a floating-point metric. Every set field is ANDed.")
class FloatComparison:
    eq: float | None = None
    gt: float | None = None
    gte: float | None = None
    lt: float | None = None
    lte: float | None = None


@strawberry.input(
    description=(
        "Compare one or more aggregates of a numeric field. Every set field is "
        "ANDed, so `{sum: {gt: 100}, avg: {lt: 30}}` keeps only the groups "
        "satisfying both."
    )
)
class NumericAggregateComparison:
    sum: FloatComparison | None = None
    avg: FloatComparison | None = None
    min: FloatComparison | None = None
    max: FloatComparison | None = None


# ---------------------------------------------------------------------------
# Per-entity having inputs
# ---------------------------------------------------------------------------

_COMPOSITION = (
    "Conditions set directly on this object are ANDed together. `and` nests "
    "further groups, each ANDed in; `or` nests groups of which at least one "
    "must hold."
)


@strawberry.input(description=f"Filter calendar event groups by their aggregates. {_COMPOSITION}")
class CalendarEventHavingInput:
    count: IntComparison | None = None
    duration_minutes: NumericAggregateComparison | None = None
    attendances_count: IntComparison | None = None
    external_attendances_count: IntComparison | None = None
    resource_allocations_count: IntComparison | None = None
    and_: list["CalendarEventHavingInput"] | None = strawberry.field(default=None, name="and")
    or_: list["CalendarEventHavingInput"] | None = strawberry.field(default=None, name="or")


@strawberry.input(description=f"Filter available time groups by their aggregates. {_COMPOSITION}")
class AvailableTimeHavingInput:
    count: IntComparison | None = None
    duration_minutes: NumericAggregateComparison | None = None
    and_: list["AvailableTimeHavingInput"] | None = strawberry.field(default=None, name="and")
    or_: list["AvailableTimeHavingInput"] | None = strawberry.field(default=None, name="or")


@strawberry.input(description=f"Filter blocked time groups by their aggregates. {_COMPOSITION}")
class BlockedTimeHavingInput:
    count: IntComparison | None = None
    duration_minutes: NumericAggregateComparison | None = None
    and_: list["BlockedTimeHavingInput"] | None = strawberry.field(default=None, name="and")
    or_: list["BlockedTimeHavingInput"] | None = strawberry.field(default=None, name="or")


@strawberry.input(description=f"Filter appointment type groups by their aggregates. {_COMPOSITION}")
class AppointmentTypeHavingInput:
    count: IntComparison | None = None
    duration_minutes: NumericAggregateComparison | None = None
    events_count: IntComparison | None = None
    slots_count: IntComparison | None = None
    and_: list["AppointmentTypeHavingInput"] | None = strawberry.field(default=None, name="and")
    or_: list["AppointmentTypeHavingInput"] | None = strawberry.field(default=None, name="or")


@strawberry.input(description=f"Filter calendar groups by their aggregates. {_COMPOSITION}")
class CalendarHavingInput:
    count: IntComparison | None = None
    capacity: NumericAggregateComparison | None = None
    events_count: IntComparison | None = None
    blocked_times_count: IntComparison | None = None
    available_times_count: IntComparison | None = None
    and_: list["CalendarHavingInput"] | None = strawberry.field(default=None, name="and")
    or_: list["CalendarHavingInput"] | None = strawberry.field(default=None, name="or")


@strawberry.input(description=f"Filter calendar pool groups by their aggregates. {_COMPOSITION}")
class CalendarPoolHavingInput:
    count: IntComparison | None = None
    memberships_count: IntComparison | None = None
    and_: list["CalendarPoolHavingInput"] | None = strawberry.field(default=None, name="and")
    or_: list["CalendarPoolHavingInput"] | None = strawberry.field(default=None, name="or")


#: The having input each entity's aggregate field accepts.
HAVING_INPUT_TYPES: Mapping[AggregatableEntity, type] = {
    AggregatableEntity.CALENDAR_EVENT: CalendarEventHavingInput,
    AggregatableEntity.AVAILABLE_TIME: AvailableTimeHavingInput,
    AggregatableEntity.BLOCKED_TIME: BlockedTimeHavingInput,
    AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeHavingInput,
    AggregatableEntity.CALENDAR: CalendarHavingInput,
    AggregatableEntity.CALENDAR_POOL: CalendarPoolHavingInput,
}

type AnyHavingInput = (
    CalendarEventHavingInput
    | AvailableTimeHavingInput
    | BlockedTimeHavingInput
    | AppointmentTypeHavingInput
    | CalendarHavingInput
    | CalendarPoolHavingInput
)

NESTING_TOO_DEEP_MESSAGE = "A having clause may not nest more than 5 levels deep"

#: How far ``and`` / ``or`` may nest. A tree is cheap to evaluate but not free to
#: build, and the schema's own depth limiter counts output selections rather
#: than input nesting, so this is the only thing bounding it.
MAX_HAVING_DEPTH = 5


def _conditions_for(reference: MetricReference, comparison: object) -> list[HavingCondition]:
    """Every condition one comparison object contributes, for one metric."""
    conditions: list[HavingCondition] = []
    for name, operator in OPERATOR_FOR_COMPARISON.items():
        value = getattr(comparison, name, None)
        if value is None:
            continue
        conditions.append(
            HavingCondition(alias=reference.alias, operator=operator, value=float(value))
        )
    return conditions


def _collect(
    entity: AggregatableEntity,
    having_input: AnyHavingInput,
    required: dict[str, MetricReference],
    depth: int,
) -> HavingSpec:
    """Turn one having input into a spec, recording the metrics it needs.

    Driven by the registry rather than by the input object's attributes, so a
    field added to a registration and to its having input is picked up here
    without a third edit.
    """
    if depth > MAX_HAVING_DEPTH:
        raise InvalidAggregatePlanError(NESTING_TOO_DEEP_MESSAGE)

    registration = get_registration(entity)
    references = referenceable_metrics(entity)
    conditions: list[HavingCondition] = []

    def take(reference_name: str, attribute: str, op: AggregateOp | None = None) -> None:
        comparison = getattr(having_input, attribute, None)
        if comparison is None:
            return
        if op is not None:
            comparison = getattr(comparison, op.value.lower(), None)
            if comparison is None:
                return
        reference = references[reference_name]
        found = _conditions_for(reference, comparison)
        if found:
            required.setdefault(reference.alias, reference)
            conditions.extend(found)

    take(COUNT_METRIC_NAME, COUNT_METRIC_NAME)

    for field_name, registered in registration.aggregatable.items():
        if registered.kind is not FieldKind.NUMERIC:
            continue
        for sub_name, op in OP_FOR_NUMERIC_COMPARISON.items():
            if op not in registered.operations:
                continue
            take(f"{field_name}_{sub_name}", field_name, op)

    for relation_name in registration.relation_counts:
        take(f"{relation_name}_count", f"{relation_name}_count")

    all_of = tuple(
        _collect(entity, nested, required, depth + 1)
        for nested in (getattr(having_input, "and_", None) or ())
    )
    any_of = tuple(
        _collect(entity, nested, required, depth + 1)
        for nested in (getattr(having_input, "or_", None) or ())
    )

    return HavingSpec(conditions=tuple(conditions), all_of=all_of, any_of=any_of)


def resolve_having(
    entity: AggregatableEntity, having_input: AnyHavingInput | None
) -> tuple[HavingSpec | None, tuple[MetricSpec, ...]]:
    """Turn a having input into a plan spec plus the metrics it needs annotated.

    Returns ``(None, ())`` for an absent clause, which is what keeps a query
    without ``having`` byte-identical to one from before this phase existed.
    """
    if having_input is None:
        return None, ()

    required: dict[str, MetricReference] = {}
    spec = _collect(entity, having_input, required, depth=1)
    return spec, tuple(reference.to_metric() for reference in required.values())


def having_is_empty(spec: HavingSpec) -> bool:
    """True when a spec carries no condition anywhere in its tree.

    ``having: {}`` is a legal document and means "no constraint"; passing an
    empty tree to the executor would add an empty ``Q()`` that filters nothing,
    so this lets the resolver drop it instead.
    """
    if spec.conditions:
        return False
    return all(having_is_empty(child) for child in (*spec.all_of, *spec.any_of))


def flatten_conditions(spec: HavingSpec) -> Sequence[HavingCondition]:
    """Every condition in a tree, in no particular order. For tests and audit."""
    found = list(spec.conditions)
    for child in (*spec.all_of, *spec.any_of):
        found.extend(flatten_conditions(child))
    return found
