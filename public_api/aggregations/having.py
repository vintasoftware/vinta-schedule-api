"""Filtering groups on what they aggregated to -- the GraphQL half of ``HAVING``.

A ``filter`` narrows the rows that go *into* a group; a ``having`` drops whole
groups on the strength of the number that came *out* of one. "Calendars with
more than twenty events" is only expressible as the second, and it is the
difference between "all calendars with their counts" and "the busy ones".

Three things live here.

**The comparison inputs.** One per scalar type (``IntComparison`` and friends),
then one per aggregate type (``NumericAggregateComparison`` and friends) whose
fields are the operations that aggregate type offers. The nesting mirrors the
output side exactly -- ``durationMinutes { sum }`` comes back through
``NumericAggregate.sum``, and is filtered through
``NumericAggregateComparison.sum`` -- so a partner reads one shape in both
directions.

**The per-entity ``*HavingInput``**, generated from the registry for the same
reason the row types are: the set of things you may filter a group on is
exactly the set of things a group computes, and writing that out twice is how
the two stop matching. ``concat`` is the one aggregate with no comparison --
its row key depends on the separator and distinctness it was called with, so a
bare reference to it does not name a column.

**The conversion**, :func:`having_from_input`, which returns a ``Q`` *and* the
metrics that ``Q`` needs. That pairing is the whole trick: a caller may filter
on ``count`` without selecting it, and the metric has to be annotated anyway or
Django resolves the alias against the model and silently filters in ``WHERE``.
The executor refuses a predicate whose aliases it cannot see
(:class:`~public_api.aggregations.errors.UnknownHavingAliasError`), so the two
halves are checked against each other rather than trusted.

``and`` / ``or`` compose, and both are Python keywords, so they are declared as
``and_`` / ``or_`` and renamed on the way into the schema.
"""

import datetime
from collections.abc import Mapping, Sequence
from typing import Any

from django.db.models import Q

import strawberry

from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    HavingSpec,
    MetricSpec,
)
from public_api.aggregations.registry import (
    REGISTRY,
    AggregateKind,
    EntityRegistration,
    get_registration,
    metric_alias,
)


# The row alias the group's own count lands under. Declared by
# `public_api.aggregations.fields`, repeated here as the one string both
# modules have to agree on for `having: {count: ...}` to mean anything.
ROW_COUNT_ALIAS = "count"

# GraphQL comparison field -> ORM lookup suffix. `eq` is spelled out rather
# than left implicit so every comparison reads the same way.
_LOOKUP_BY_COMPARISON_FIELD: Mapping[str, str] = {
    "eq": "exact",
    "gt": "gt",
    "gte": "gte",
    "lt": "lt",
    "lte": "lte",
}


# ---------------------------------------------------------------------------
# Scalar comparisons
# ---------------------------------------------------------------------------


@strawberry.input(description="Compare an integer aggregate. Set one or more; all apply.")
class IntComparison:
    eq: int | None = None
    gt: int | None = None
    gte: int | None = None
    lt: int | None = None
    lte: int | None = None


@strawberry.input(description="Compare a floating-point aggregate. Set one or more; all apply.")
class FloatComparison:
    eq: float | None = None
    gt: float | None = None
    gte: float | None = None
    lt: float | None = None
    lte: float | None = None


@strawberry.input(description="Compare a string aggregate. Set one or more; all apply.")
class StringComparison:
    eq: str | None = None
    gt: str | None = None
    gte: str | None = None
    lt: str | None = None
    lte: str | None = None


@strawberry.input(description="Compare a datetime aggregate. Set one or more; all apply.")
class DateTimeComparison:
    eq: datetime.datetime | None = None
    gt: datetime.datetime | None = None
    gte: datetime.datetime | None = None
    lt: datetime.datetime | None = None
    lte: datetime.datetime | None = None


# ---------------------------------------------------------------------------
# Per-aggregate-type comparisons
# ---------------------------------------------------------------------------
#
# One field per operation the matching output type publishes, so the filter
# side and the read side name the same things. `concat` is absent by
# construction: its row key carries the separator and distinctness it was built
# with, so "the concat" is not a single column to compare against.


@strawberry.input(description="Filter a group on a numeric field's aggregates.")
class NumericAggregateComparison:
    sum: FloatComparison | None = None
    avg: FloatComparison | None = None
    min: FloatComparison | None = None
    max: FloatComparison | None = None


@strawberry.input(
    description=(
        "Filter a group on a string field's aggregates. `concat` is not "
        "comparable: its value depends on the separator it was requested with."
    )
)
class StringAggregateComparison:
    min: StringComparison | None = None
    max: StringComparison | None = None


@strawberry.input(description="Filter a group on a date or datetime field's aggregates.")
class DateTimeAggregateComparison:
    min: DateTimeComparison | None = None
    max: DateTimeComparison | None = None


@strawberry.input(description="Filter a group on a boolean field's counts.")
class BooleanAggregateComparison:
    true_count: IntComparison | None = None
    false_count: IntComparison | None = None


# Which comparison input each aggregate kind is filtered through, and which
# operation each of that input's fields refers to.
_COMPARISON_TYPE_BY_KIND: Mapping[AggregateKind, type] = {
    AggregateKind.NUMERIC: NumericAggregateComparison,
    AggregateKind.STRING: StringAggregateComparison,
    AggregateKind.DATETIME: DateTimeAggregateComparison,
    AggregateKind.BOOLEAN: BooleanAggregateComparison,
}

_OP_BY_COMPARISON_FIELD: Mapping[str, AggregateOp] = {
    "sum": AggregateOp.SUM,
    "avg": AggregateOp.AVG,
    "min": AggregateOp.MIN,
    "max": AggregateOp.MAX,
    "true_count": AggregateOp.TRUE_COUNT,
    "false_count": AggregateOp.FALSE_COUNT,
}


# ---------------------------------------------------------------------------
# Per-entity having inputs
# ---------------------------------------------------------------------------


def _entity_class_prefix(entity: AggregatableEntity) -> str:
    """`calendar_event` -> `CalendarEvent`."""
    return "".join(part.title() for part in entity.value.split("_"))


def _build_having_input(registration: EntityRegistration) -> type:
    """Build one entity's `*HavingInput` from its registration.

    Self-referential through `and` / `or`, which is why the two list
    annotations are strings: the class does not exist yet when they are
    written, and Strawberry resolves an annotation against its class's module
    namespace at schema-build time -- by which point the name below has been
    published into this module's globals.
    """
    class_name = f"{_entity_class_prefix(registration.entity)}HavingInput"

    annotations: dict[str, Any] = {"count": IntComparison | None}
    namespace: dict[str, Any] = {
        "count": strawberry.field(
            default=None, description="Filter on how many rows fell into the group."
        )
    }

    for spec in registration.aggregatable:
        annotations[spec.name] = _COMPARISON_TYPE_BY_KIND[spec.kind] | None
        namespace[spec.name] = strawberry.field(default=None, description=spec.description or None)

    for relation in registration.relation_counts:
        annotations[relation.name] = IntComparison | None
        namespace[relation.name] = strawberry.field(
            default=None, description=relation.description or None
        )

    annotations["and_"] = f"list[{class_name}] | None"
    namespace["and_"] = strawberry.field(
        default=None, name="and", description="Every nested condition must hold."
    )
    annotations["or_"] = f"list[{class_name}] | None"
    namespace["or_"] = strawberry.field(
        default=None, name="or", description="At least one nested condition must hold."
    )

    namespace["__annotations__"] = annotations
    namespace["__module__"] = __name__
    namespace["__doc__"] = f"Conditions on a {_entity_class_prefix(registration.entity)} group."

    input_class = type(class_name, (), namespace)
    decorated = strawberry.input(
        input_class,
        description=(
            f"Drop {_entity_class_prefix(registration.entity)} groups whose aggregates do "
            f"not match. Fields set alongside each other all apply."
        ),
    )
    # Published so the `and` / `or` string annotations above resolve.
    globals()[class_name] = decorated
    return decorated


HAVING_INPUT_TYPE_BY_ENTITY: Mapping[AggregatableEntity, type] = {
    entity: _build_having_input(registration) for entity, registration in REGISTRY.items()
}


# ---------------------------------------------------------------------------
# Input -> predicate
# ---------------------------------------------------------------------------


def _comparison_terms(alias: str, comparison: Any) -> list[Q]:
    """One `Q` per operator set on a scalar comparison.

    Several operators on one comparison read as a range and are ANDed, which is
    what `{gte: 10, lt: 20}` plainly means.
    """
    terms: list[Q] = []
    for field_name, lookup in _LOOKUP_BY_COMPARISON_FIELD.items():
        value = getattr(comparison, field_name, None)
        if value is None:
            continue
        terms.append(Q(**{f"{alias}__{lookup}": value}))
    return terms


def _aggregate_terms(
    field_name: str, comparison: Any
) -> tuple[list[Q], list[tuple[str, AggregateOp]]]:
    """The conditions one aggregate-type comparison contributes, and what they need.

    Returns the `Q` terms alongside the `(field, operation)` pairs they read,
    so the caller can make sure each one is annotated.
    """
    terms: list[Q] = []
    required: list[tuple[str, AggregateOp]] = []
    for comparison_field, op in _OP_BY_COMPARISON_FIELD.items():
        scalar = getattr(comparison, comparison_field, None)
        if scalar is None:
            continue
        alias = metric_alias(field_name, op)
        operator_terms = _comparison_terms(alias, scalar)
        if not operator_terms:
            # The operation was named with no operator under it -- nothing to
            # filter on, and nothing to annotate for.
            continue
        terms.extend(operator_terms)
        required.append((field_name, op))
    return terms, required


def _predicate_from_input(
    registration: EntityRegistration, having_input: Any, required: dict[str, MetricSpec]
) -> Q | None:
    """One having input as a `Q`, recording the metrics it needs in `required`.

    `required` is keyed by alias, so a condition and a selection that both want
    the same metric produce one annotation rather than a duplicate-alias
    collision.
    """
    terms: list[Q] = []

    row_count = getattr(having_input, ROW_COUNT_ALIAS, None)
    if row_count is not None:
        count_terms = _comparison_terms(ROW_COUNT_ALIAS, row_count)
        if count_terms:
            terms.extend(count_terms)
            required.setdefault(ROW_COUNT_ALIAS, MetricSpec.row_count(alias=ROW_COUNT_ALIAS))

    for spec in registration.aggregatable:
        comparison = getattr(having_input, spec.name, None)
        if comparison is None:
            continue
        field_terms, needed = _aggregate_terms(spec.name, comparison)
        terms.extend(field_terms)
        for field_name, op in needed:
            alias = metric_alias(field_name, op)
            required.setdefault(alias, MetricSpec(alias=alias, field_path=field_name, op=op))

    for relation in registration.relation_counts:
        comparison = getattr(having_input, relation.name, None)
        if comparison is None:
            continue
        alias = metric_alias(relation.name, AggregateOp.COUNT)
        relation_terms = _comparison_terms(alias, comparison)
        if not relation_terms:
            continue
        terms.extend(relation_terms)
        required.setdefault(
            alias, MetricSpec(alias=alias, field_path=relation.name, op=AggregateOp.COUNT)
        )

    for attribute, connector in (("and_", Q.AND), ("or_", Q.OR)):
        nested = _combine(
            [
                _predicate_from_input(registration, child, required)
                for child in _nested(having_input, attribute)
            ],
            connector,
        )
        if nested is not None:
            terms.append(nested)

    return _combine(terms, Q.AND)


def _nested(having_input: Any, attribute: str) -> Sequence[Any]:
    """The `and` / `or` children of an input, an omitted list reading as none."""
    return getattr(having_input, attribute, None) or ()


def _combine(terms: Sequence[Q | None], connector: str) -> Q | None:
    """Fold `terms` together, or `None` when none of them said anything.

    `None` rather than an empty `Q()`: an empty `Q` filters nothing but still
    makes the caller believe a `having` was applied, and the executor's own
    "was a having asked for" check reads the plan, not the SQL.
    """
    present = [term for term in terms if term is not None]
    if not present:
        return None
    combined = present[0]
    for term in present[1:]:
        combined = Q(combined, term, _connector=connector)
    return combined


def having_from_input(
    registration: EntityRegistration, having_input: Any
) -> tuple[HavingSpec | None, tuple[MetricSpec, ...]]:
    """Turn a field's `having` argument into a predicate and the metrics it reads.

    Both halves are returned together because they are two views of one
    decision. A `having` on a metric the document did not select still has to
    annotate that metric -- otherwise the alias resolves against the model, and
    Django renders the condition as a `WHERE` over a column, which filters rows
    before grouping instead of groups after it. That is a wrong answer rather
    than an error, which is why the executor also refuses a predicate whose
    aliases it cannot account for.
    """
    if having_input is None:
        return None, ()

    required: dict[str, MetricSpec] = {}
    predicate = _predicate_from_input(registration, having_input, required)
    if predicate is None:
        # A `having` that named nothing. Treated as absent rather than as an
        # empty predicate, so `plan.having` means "the caller filtered".
        return None, ()
    return HavingSpec(predicate=predicate), tuple(required.values())


def having_input_type(entity: AggregatableEntity) -> type:
    """The `*HavingInput` for `entity`, raising rather than returning `None`."""
    get_registration(entity)
    return HAVING_INPUT_TYPE_BY_ENTITY[entity]


# Every generated having input, exposed under its own name for a caller that
# wants one without going through the entity mapping.
__all__ = [
    "HAVING_INPUT_TYPE_BY_ENTITY",
    "ROW_COUNT_ALIAS",
    "BooleanAggregateComparison",
    "DateTimeAggregateComparison",
    "DateTimeComparison",
    "FloatComparison",
    "IntComparison",
    "NumericAggregateComparison",
    "StringAggregateComparison",
    "StringComparison",
    "having_from_input",
    "having_input_type",
]
