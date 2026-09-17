"""Turn an :class:`~public_api.aggregations.plan.AggregateQueryPlan` into one queryset.

The whole aggregate runs as SQL the Django ORM generated: one
``.values(...).annotate(...)`` chain, one round trip, and no Python that sums,
sorts or buckets a row. A row comes back from the database already grouped and
already aggregated; the only thing left for a resolver to do is map a dict onto
a Strawberry type.

**Tenancy is the caller's, and stays the caller's.** The base queryset arrives
as an argument and must already come from the model's organization-scoped
manager. Nothing in this module reaches for ``original_manager`` or
``unscoped()``: a ``GROUP BY`` over an unscoped queryset returns numbers that
include another tenant's rows and looks exactly like a correct answer.

Two constructions here are worth reading before changing them.

*Grouping.* Dimension expressions are annotated first, then named in
``.values()``, then the aggregates are annotated. That order is what makes the
``.values()`` list the ``GROUP BY`` list; annotating the aggregates before the
``.values()`` call would group by every column instead.

*Relation counts.* Several ``Count()`` calls over different relations in one
``annotate()`` fan the joins out and multiply each count by the others' row
counts. Each relation count is therefore computed per row as a correlated
subquery *before* the grouping, and summed inside it -- the same shape
``childOrganizations`` uses in ``public_api/queries.py``.
"""

from collections.abc import Mapping
from typing import Any

from django.db.models import (
    Avg,
    Count,
    F,
    Max,
    Min,
    OuterRef,
    Q,
    QuerySet,
    StringAgg,
    Subquery,
    Sum,
    Value,
)
from django.db.models.expressions import Combinable
from django.db.models.functions import Coalesce, TruncDay, TruncMonth, TruncWeek

from public_api.aggregations.errors import (
    AggregateRegistrationError,
    EntityQuerysetMismatchError,
    ReservedAliasError,
    UnsupportedAggregateOperationError,
    WindowNotSupportedError,
)
from public_api.aggregations.plan import (
    AggregateOp,
    AggregateQueryPlan,
    DimensionSpec,
    MetricSpec,
)
from public_api.aggregations.registry import (
    AggregatableField,
    EntityRegistration,
    RelationCount,
    reverse_relation_fk_attname,
    validate_plan,
)
from public_api.aggregations.types import DEFAULT_CONCAT_SEPARATOR, TemporalGranularity


# Bucketing functions, one per granularity. All three take the caller's
# ``tzinfo``, so the bucket boundary is a wall clock the caller named rather
# than the server's or the row's.
_TRUNC_BY_GRANULARITY = {
    TemporalGranularity.DAY: TruncDay,
    TemporalGranularity.WEEK: TruncWeek,
    TemporalGranularity.MONTH: TruncMonth,
}

# Prefix for the per-row relation counts annotated before the grouping. Not a
# row key: these are summed into the caller's alias and never selected.
_RELATION_COUNT_PREFIX = "_aggregate_relation_"


def build_aggregate_queryset(
    plan: AggregateQueryPlan, queryset: QuerySet
) -> QuerySet[Any, dict[str, Any]]:
    """Build the grouped, aggregated queryset ``plan`` describes.

    ``queryset`` is the already-narrowed, already-organization-scoped base to
    aggregate over: whatever the entity's filter input produced. It is used as
    given -- this function narrows nothing and widens nothing.

    The returned queryset yields dictionaries keyed by the plan's aliases, and
    is sliced to the plan's ``offset`` / ``limit``. It has not been evaluated.
    """
    registration = validate_plan(plan)

    if plan.window is not None:
        raise WindowNotSupportedError

    if queryset.model is not registration.model:
        raise EntityQuerysetMismatchError(plan.entity, registration.model, queryset.model)

    # Built once rather than per alias: every annotation name is checked against
    # it, and walking the model's fields for each one is the same answer several
    # times over.
    column_names = _column_names(registration)

    pre_annotations: dict[str, Combinable] = {}
    dimension_annotations: dict[str, Combinable] = {}
    group_by: list[str] = []

    for dimension in plan.dimensions:
        expression = _dimension_expression(registration, dimension)
        if expression is None:
            # Alias and column agree, so the column can be named directly and
            # no annotation (and no alias collision) is possible.
            group_by.append(dimension.alias)
            continue
        _reject_reserved_alias(registration, dimension.alias, column_names)
        dimension_annotations[dimension.alias] = expression
        group_by.append(dimension.alias)

    aggregates: dict[str, Combinable] = {}
    for metric in plan.metrics:
        _reject_reserved_alias(registration, metric.alias, column_names)
        relation = _relation_count_for(registration, metric)
        if relation is not None:
            hidden_alias = f"{_RELATION_COUNT_PREFIX}{metric.alias}"
            pre_annotations[hidden_alias] = _relation_count_subquery(registration, relation)
            aggregates[metric.alias] = Sum(hidden_alias)
            continue
        aggregates[metric.alias] = _aggregate_expression(registration, metric)

    qs = queryset
    if pre_annotations:
        qs = qs.annotate(**pre_annotations)
    if dimension_annotations:
        qs = qs.annotate(**dimension_annotations)

    grouped = qs.values(*group_by).annotate(**aggregates)

    if plan.having is not None:
        # Applied after ``.annotate()``, which is what makes Django render it
        # as ``HAVING`` rather than folding it into the ``WHERE`` clause.
        grouped = grouped.filter(plan.having.predicate)

    grouped = grouped.order_by(*_order_by(plan, group_by))
    return grouped[plan.offset : plan.offset + plan.limit]


def execute_plan(plan: AggregateQueryPlan, queryset: QuerySet) -> list[dict[str, Any]]:
    """Run ``plan`` and return its rows, one dict per group.

    One database query. Rows arrive grouped and aggregated; nothing here
    reshapes them.
    """
    return list(build_aggregate_queryset(plan, queryset))


def _order_by(plan: AggregateQueryPlan, group_by: list[str]) -> list[str]:
    """The ordering to apply, defaulting to the group key.

    A grouped result with no ``ORDER BY`` comes back in whatever order the
    database found convenient, which makes ``offset`` / ``limit`` paging return
    overlapping and missing groups between two otherwise identical calls.
    Ordering on the full group key is both deterministic and free -- it is the
    set of columns already being grouped on.
    """
    if not plan.order_by:
        return group_by
    ordered = [spec.as_order_by() for spec in plan.order_by]
    # Append whatever the caller did not name, so ties inside their ordering
    # still break the same way every time.
    named = {spec.alias for spec in plan.order_by}
    ordered.extend(alias for alias in group_by if alias not in named)
    return ordered


def _dimension_expression(
    registration: EntityRegistration, dimension: DimensionSpec
) -> Combinable | None:
    """The expression to group on, or ``None`` when the raw column will do.

    ``None`` is the common case for a scalar dimension whose alias matches its
    column: naming the column in ``.values()`` groups on it directly, with no
    annotation to collide with a model field.
    """
    spec = registration.groupable_field(dimension.field_path)

    if dimension.granularity is not None:
        trunc = _TRUNC_BY_GRANULARITY[dimension.granularity]
        return trunc(spec.field_path, tzinfo=dimension.tzinfo)

    if dimension.alias == spec.field_path:
        return None

    # ``F`` over a foreign key resolves to the key column, so grouping by
    # ``calendar_id`` reads ``calendar_fk_id`` without joining the calendar in.
    return F(spec.field_path)


def _relation_count_for(
    registration: EntityRegistration, metric: MetricSpec
) -> RelationCount | None:
    """The relation this metric counts, or ``None`` for a plain row count."""
    if metric.op is not AggregateOp.COUNT:
        return None
    return registration.relation_count(metric.field_path)


def _relation_count_subquery(
    registration: EntityRegistration, relation: RelationCount
) -> Combinable:
    """One related row count per row of the model being aggregated.

    The related model's *default manager* is used, so the subquery carries the
    organization filter the bound context implies -- the reason this never
    needs ``original_manager``, and the reason the correlation may be written
    against the concrete key column rather than the organization-safe relation:
    there is no join whose ``ON`` clause could lose the organization.

    ``Coalesce(..., 0)`` matters: a parent with no related rows produces no
    subquery row at all, and summing ``NULL`` into a group would make the whole
    group's count ``NULL`` rather than smaller.
    """
    related_model = registration.model._meta.get_field(relation.relation).related_model
    if related_model is None:
        # Unreachable through the registry, which resolves the same relation at
        # import time; kept so the failure names the relation rather than being
        # an attribute error on ``None``.
        raise AggregateRegistrationError(
            f"{registration.model.__name__}.{relation.relation} has no related model"
        )
    fk_attname = reverse_relation_fk_attname(registration.model, relation.relation)

    # ``_default_manager`` is ``objects`` on every model registered here --
    # ``objects`` is declared first on each of them, and it is the
    # ``OrganizationScopedManager`` subclass. Naming it this way rather than
    # ``objects`` keeps this function generic over the related model without
    # losing the scoping; ``original_manager`` and ``unscoped()`` are the two
    # this must never reach for.
    per_parent = (
        related_model._default_manager.filter(**{fk_attname: OuterRef("pk")})
        .values(fk_attname)
        .annotate(related_row_count=Count("id"))
        .values("related_row_count")
    )
    return Coalesce(Subquery(per_parent), Value(0))


def _aggregate_expression(registration: EntityRegistration, metric: MetricSpec) -> Combinable:
    """The aggregate function for one metric."""
    if metric.op is AggregateOp.COUNT:
        # ``Count("id")``, as the plan prescribes: counting the primary key
        # counts rows, and counts them whatever else the row holds.
        return Count("id", distinct=bool(metric.options.get("distinct", False)))

    spec = registration.aggregatable_field(metric.field_path)
    source = _metric_source(spec)

    if metric.op is AggregateOp.SUM:
        return Sum(source)
    if metric.op is AggregateOp.AVG:
        return Avg(source)
    if metric.op is AggregateOp.MIN:
        return Min(source)
    if metric.op is AggregateOp.MAX:
        return Max(source)
    if metric.op is AggregateOp.CONCAT:
        return _string_agg(source, metric.options)
    if metric.op is AggregateOp.TRUE_COUNT:
        return Count("id", filter=Q(**{spec.field_path: True}))
    if metric.op is AggregateOp.FALSE_COUNT:
        return Count("id", filter=Q(**{spec.field_path: False}))

    raise UnsupportedAggregateOperationError(registration.entity, metric.field_path, metric.op)


def _metric_source(spec: AggregatableField) -> Combinable | str:
    """What to aggregate: a derived expression, or the column's own path."""
    if spec.expression is not None:
        return spec.expression
    return spec.field_path


def _string_agg(source: Combinable | str, options: Mapping[str, Any]) -> Combinable:
    """``string_agg`` over the group, ordered so repeated runs agree.

    Postgres leaves ``string_agg``'s order undefined, which would make the same
    query return the same titles in a different order between two calls.
    Ordering by the aggregated value itself is deterministic and is also the
    only ordering compatible with ``DISTINCT``, which Postgres requires to
    match the ``ORDER BY`` expression.
    """
    separator = options.get("separator", DEFAULT_CONCAT_SEPARATOR)
    return StringAgg(
        source,
        Value(separator),
        distinct=bool(options.get("distinct", False)),
        order_by=source,
    )


def _column_names(registration: EntityRegistration) -> frozenset[str]:
    """Every name the model already answers to, as field names and attnames."""
    names: set[str] = set()
    for model_field in registration.model._meta.get_fields():
        names.add(model_field.name)
        attname = getattr(model_field, "attname", None)
        if attname:
            names.add(attname)
    return frozenset(names)


def _reject_reserved_alias(
    registration: EntityRegistration, alias: str, column_names: frozenset[str]
) -> None:
    """Refuse an alias that shadows a column of the model being grouped.

    Django raises on an annotation whose name matches a model field, with a
    message about the model rather than about the aggregate. Catching it here
    names the alias to rename instead.
    """
    if alias in column_names:
        raise ReservedAliasError(alias, registration.model.__name__)
