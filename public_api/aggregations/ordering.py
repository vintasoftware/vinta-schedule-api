"""Ordering grouped rows -- by a dimension of the key, or by a metric.

Ordering is what turns a bounded result into a *useful* bounded one. Without
it, `limit: 10` over a hundred calendars returns ten calendars the database
found convenient; with `orderBy: [{metric: COUNT, direction: DESC}]` it returns
the ten busiest. Top-N is not a separate feature -- it is this plus the limit
Phase 3 already caps.

Two things a caller may order by, and they are different enough to be separate
fields rather than one string:

* **A key dimension.** Named as the registry names it, and resolved against the
  dimensions *this query grouped by* -- which is what supplies the alias, since
  a bucketed dimension's row key carries its granularity (`start_time_day`).
  Naming a dimension the query did not group by is refused
  (:class:`~public_api.aggregations.errors.UngroupedOrderKeyError`) rather than
  passed through: Django would resolve it against the model, add a column to
  the `GROUP BY`, and return numbers split more finely than the query asked
  for.
* **A metric.** Named by a generated enum whose *value is the row alias*, so
  resolving one is a lookup rather than a reconstruction. A metric ordered by
  but not selected is annotated anyway, the same way a `having` on an
  unselected metric is -- the alias has to exist for `ORDER BY` to mean the
  aggregate rather than a column.

`concat` has no member in the metric enum. Its row key depends on the separator
and distinctness it was requested with, so a bare `TITLE_CONCAT` would not name
one column.

Exactly one of the two must be set. GraphQL has no input unions, so that is the
one part of the contract the schema cannot carry -- the same gap, and the same
resolution, as `groupBy`'s scalar/temporal split.

Determinism is not this module's to arrange: the executor appends every group
key the caller did not name to whatever ordering it is given
(`executor._order_by`), so ties inside a metric ordering break the same way on
every run and paging never returns a group twice.
"""

import enum
from collections.abc import Mapping, Sequence
from typing import Any

import strawberry

from public_api.aggregations.errors import (
    AmbiguousOrderInputError,
    UngroupedOrderKeyError,
)
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    DimensionSpec,
    MetricSpec,
    OrderSpec,
)
from public_api.aggregations.registry import (
    REGISTRY,
    EntityRegistration,
    get_registration,
    metric_alias,
)


# The row alias the group's own count lands under, shared with
# `public_api.aggregations.having`.
ROW_COUNT_ALIAS = "count"

# `concat` takes arguments that change its row key, so it cannot be named by a
# bare enum member. Every other operation maps one-to-one onto an alias.
_UNORDERABLE_OPS = frozenset({AggregateOp.CONCAT})


@strawberry.enum(description="Which way to sort.")
class OrderDirection(enum.Enum):
    ASC = "ASC"
    DESC = "DESC"


def _entity_class_prefix(entity: AggregatableEntity) -> str:
    """`calendar_event` -> `CalendarEvent`."""
    return "".join(part.title() for part in entity.value.split("_"))


# ---------------------------------------------------------------------------
# Generated enums
# ---------------------------------------------------------------------------


def _build_order_key_enum(registration: EntityRegistration) -> type[enum.Enum]:
    """Every dimension this entity may be grouped by, as one enum.

    Deliberately the union of the scalar and temporal group-by enums rather
    than a third hand-written list: a dimension you can group by is a dimension
    you can sort on, and generating it from the same registration is what stops
    the two drifting.
    """
    members = {spec.name.upper(): spec.name for spec in registration.groupable}
    key_enum = enum.Enum(  # type: ignore[misc]
        f"{_entity_class_prefix(registration.entity)}AggregateOrderKey", members
    )
    return strawberry.enum(
        key_enum,
        description=(
            f"A {_entity_class_prefix(registration.entity)} group-key dimension to sort "
            f"on. It must be one the query also grouped by."
        ),
    )


def _metric_members(registration: EntityRegistration) -> dict[str, str]:
    """Enum member name -> row alias, for every metric that has a stable alias."""
    members: dict[str, str] = {"COUNT": ROW_COUNT_ALIAS}
    for spec in registration.aggregatable:
        for op in sorted(spec.ops, key=lambda candidate: candidate.value):
            if op in _UNORDERABLE_OPS:
                continue
            members[f"{spec.name}_{op.value}".upper()] = metric_alias(spec.name, op)
    for relation in registration.relation_counts:
        members[relation.name.upper()] = metric_alias(relation.name, AggregateOp.COUNT)
    return members


def _build_metric_ref_enum(registration: EntityRegistration) -> type[enum.Enum]:
    """Every metric this entity can be sorted by, keyed by its own row alias."""
    metric_enum = enum.Enum(  # type: ignore[misc]
        f"{_entity_class_prefix(registration.entity)}AggregateMetricRef",
        _metric_members(registration),
    )
    return strawberry.enum(
        metric_enum,
        description=(
            f"A {_entity_class_prefix(registration.entity)} metric to sort on. Selecting "
            f"it is not required -- a metric ordered by is computed either way."
        ),
    )


ORDER_KEY_ENUM_BY_ENTITY: Mapping[AggregatableEntity, type[enum.Enum]] = {
    entity: _build_order_key_enum(registration) for entity, registration in REGISTRY.items()
}

METRIC_REF_ENUM_BY_ENTITY: Mapping[AggregatableEntity, type[enum.Enum]] = {
    entity: _build_metric_ref_enum(registration) for entity, registration in REGISTRY.items()
}


# Alias -> the metric that produces it, per entity. Built from the same members
# as the enum above, so a reference can always be turned back into something
# the plan can annotate.
def _metric_spec_by_alias(registration: EntityRegistration) -> Mapping[str, MetricSpec]:
    specs: dict[str, MetricSpec] = {ROW_COUNT_ALIAS: MetricSpec.row_count(alias=ROW_COUNT_ALIAS)}
    for spec in registration.aggregatable:
        for op in spec.ops:
            if op in _UNORDERABLE_OPS:
                continue
            alias = metric_alias(spec.name, op)
            specs[alias] = MetricSpec(alias=alias, field_path=spec.name, op=op)
    for relation in registration.relation_counts:
        alias = metric_alias(relation.name, AggregateOp.COUNT)
        specs[alias] = MetricSpec(alias=alias, field_path=relation.name, op=AggregateOp.COUNT)
    return specs


METRIC_SPEC_BY_ALIAS_BY_ENTITY: Mapping[AggregatableEntity, Mapping[str, MetricSpec]] = {
    entity: _metric_spec_by_alias(registration) for entity, registration in REGISTRY.items()
}


# ---------------------------------------------------------------------------
# Generated order inputs
# ---------------------------------------------------------------------------


def _build_order_input(registration: EntityRegistration) -> type:
    """One entity's `*AggregateOrderInput`."""
    prefix = _entity_class_prefix(registration.entity)
    annotations: dict[str, Any] = {
        "key": ORDER_KEY_ENUM_BY_ENTITY[registration.entity] | None,
        "metric": METRIC_REF_ENUM_BY_ENTITY[registration.entity] | None,
        "direction": OrderDirection,
    }
    namespace: dict[str, Any] = {
        "key": strawberry.field(default=None, description="Sort on a group-key dimension."),
        "metric": strawberry.field(default=None, description="Sort on an aggregated value."),
        "direction": strawberry.field(default=OrderDirection.DESC, description="Sort direction."),
        "__annotations__": annotations,
        "__module__": __name__,
        "__doc__": f"How to sort {prefix} aggregate rows.",
    }
    order_class = type(f"{prefix}AggregateOrderInput", (), namespace)
    return strawberry.input(
        order_class,
        description=(
            f"Sort {prefix} aggregate rows. Name exactly one of `key` or `metric`. "
            f"Whatever is not named is appended as a tiebreak, so paging is stable."
        ),
    )


ORDER_INPUT_TYPE_BY_ENTITY: Mapping[AggregatableEntity, type] = {
    entity: _build_order_input(registration) for entity, registration in REGISTRY.items()
}


# ---------------------------------------------------------------------------
# Input -> plan
# ---------------------------------------------------------------------------


def _dimension_alias_for(dimensions: Sequence[DimensionSpec], field_name: str) -> str:
    """The row key the named dimension landed under in *this* query.

    Read off the plan's own dimensions rather than recomputed, because a
    bucketed dimension's alias carries its granularity and only the query knows
    which granularity it asked for.
    """
    for dimension in dimensions:
        if dimension.field_path == field_name:
            return dimension.alias
    raise UngroupedOrderKeyError(field_name)


def order_from_input(
    entity: AggregatableEntity,
    order_by: Sequence[Any] | None,
    dimensions: Sequence[DimensionSpec],
) -> tuple[tuple[OrderSpec, ...], tuple[MetricSpec, ...]]:
    """Turn a field's `orderBy` argument into order specs and the metrics they read.

    Returns the metrics for the same reason `having_from_input` does: sorting
    on a metric the document did not select still has to annotate it, or the
    alias resolves against the model and `ORDER BY` sorts on a column instead
    of on the aggregate.
    """
    if not order_by:
        return (), ()

    # Raises `UnknownAggregateEntityError` rather than letting the mapping
    # lookup below fail with a bare `KeyError`.
    get_registration(entity)
    spec_by_alias = METRIC_SPEC_BY_ALIAS_BY_ENTITY[entity]

    specs: list[OrderSpec] = []
    required: dict[str, MetricSpec] = {}
    for entry in order_by:
        key = getattr(entry, "key", None)
        metric = getattr(entry, "metric", None)
        descending = getattr(entry, "direction", OrderDirection.DESC) is OrderDirection.DESC

        # Both, or neither. GraphQL cannot express "exactly one of", so this is
        # the one part of the contract the schema does not carry.
        if key is not None and metric is not None:
            raise AmbiguousOrderInputError
        if key is not None:
            alias = _dimension_alias_for(dimensions, key.value)
        elif metric is not None:
            alias = metric.value
            required.setdefault(alias, spec_by_alias[alias])
        else:
            raise AmbiguousOrderInputError

        specs.append(OrderSpec(alias=alias, descending=descending))

    return tuple(specs), tuple(required.values())


def order_input_type(entity: AggregatableEntity) -> type:
    """The `*AggregateOrderInput` for `entity`."""
    return ORDER_INPUT_TYPE_BY_ENTITY[entity]


__all__ = [
    "METRIC_REF_ENUM_BY_ENTITY",
    "METRIC_SPEC_BY_ALIAS_BY_ENTITY",
    "ORDER_INPUT_TYPE_BY_ENTITY",
    "ORDER_KEY_ENUM_BY_ENTITY",
    "ROW_COUNT_ALIAS",
    "OrderDirection",
    "order_from_input",
    "order_input_type",
]
