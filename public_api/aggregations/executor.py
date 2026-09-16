"""Turn an :class:`~public_api.aggregations.plan.AggregateQueryPlan` into a queryset.

Everything here is ORM-generated SQL: one ``SELECT`` with a ``GROUP BY``, no raw
SQL, no database-defined code, and no reshaping of the returned rows in Python.
What comes back is a sliced ``.values(...).annotate(...)`` queryset whose rows
are dicts keyed by the plan's aliases.

Two rules the executor never bends:

- **The base queryset is the caller's.** It arrives already organization-scoped
  through the model's ``OrganizationScopedManager`` and through the
  ``public_api.scoping`` helpers. Nothing here calls ``original_manager`` or
  ``unscoped()``, and the correlated subqueries a relation count builds go
  through the related model's organization-scoped default manager too.
- **Counts never fan out.** A related collection is counted through a
  correlated subquery, the shape ``public_api.queries.child_organizations``
  already uses, so two counts over different relations in one query cannot
  multiply each other's rows.
"""

from typing import Any

from django.db.models import (
    Avg,
    Count,
    DateTimeField,
    F,
    IntegerField,
    Max,
    Min,
    Model,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    Value,
)
from django.db.models.aggregates import StringAgg
from django.db.models.expressions import Combinable
from django.db.models.fields.reverse_related import ForeignObjectRel
from django.db.models.functions import Coalesce, Trunc

from public_api.aggregations import errors
from public_api.aggregations.plan import (
    AggregateOp,
    AggregateQueryPlan,
    DimensionSpec,
    MetricSpec,
)
from public_api.aggregations.registry import (
    EntityRegistration,
    assert_granularity_allowed,
    assert_op_supported,
    assert_organization_scoped,
    get_registration,
)


def build_aggregate_queryset(
    plan: AggregateQueryPlan, base_queryset: QuerySet[Any]
) -> QuerySet[Any]:
    """Build the grouped, aggregated, sliced queryset `plan` describes.

    Args:
        plan: The resolved request. Its aliases are the keys of every row dict.
        base_queryset: An already organization-scoped, already filtered queryset
            over the plan entity's model. Its filters -- the mandatory date
            range included -- apply to the aggregates and to the correlated
            relation counts alike, because the subqueries correlate to rows of
            this queryset.

    Returns:
        A queryset of row dicts, one per distinct group key, ordered by the
        plan's ``orderBy`` (or by the group key when it names none) and sliced
        to the plan's ``offset``/``limit``. Not yet evaluated: exactly one
        query runs, when the caller iterates it.

    Raises:
        UnknownEntityError: The plan's entity has no registration.
        UnknownFieldError: A dimension, metric or relation the plan names is
            not registered for the entity.
        UnsupportedOperationError: An operation the field's kind does not
            expose, or a granularity on a non-temporal dimension.
        AliasCollisionError: An alias shadows a column on the entity's model.
        UnsupportedPlanFeatureError: The plan carries a ``having`` or a
            ``window``, which land in later phases.
    """
    registration = get_registration(plan.entity)
    _assert_executable(plan, registration, base_queryset)

    dimensions = {spec.alias: _dimension_expression(registration, spec) for spec in plan.dimensions}
    metrics = {spec.alias: _metric_expression(registration, spec) for spec in plan.metrics}

    queryset = (
        # ``order_by()`` first: a model-level ``Meta.ordering`` would otherwise
        # be carried into the GROUP BY and split every group by the ordering
        # column. None of the six entities declares one today, and this keeps
        # that from becoming a silent correctness change if one ever does.
        base_queryset.order_by().values(**dimensions).annotate(**metrics).order_by(*_ordering(plan))
    )
    return queryset[plan.offset : plan.offset + plan.limit]


def _assert_executable(
    plan: AggregateQueryPlan,
    registration: EntityRegistration,
    base_queryset: QuerySet[Any],
) -> None:
    """Refuse a plan the executor cannot honour exactly as written."""
    if base_queryset.model is not registration.model:
        raise errors.InvalidPlanError(
            f"Plan targets {registration.model.__name__} but the base queryset is over "
            f"{base_queryset.model.__name__}"
        )
    if plan.having is not None:
        raise errors.UnsupportedPlanFeatureError(errors.HAVING_NOT_IMPLEMENTED)
    if plan.window is not None:
        raise errors.UnsupportedPlanFeatureError(errors.WINDOW_NOT_IMPLEMENTED)

    assert_organization_scoped(registration.model)
    _assert_aliases_do_not_shadow_columns(plan, registration.model)


def _assert_aliases_do_not_shadow_columns(plan: AggregateQueryPlan, model: type[Model]) -> None:
    """Refuse an alias equal to a column name on `model`.

    Django rejects such an annotation itself, with a message about a conflict
    that says nothing about aggregates. The canonical aliases
    (``public_api.aggregations.plan.dimension_alias`` / ``metric_alias``) are
    prefixed and can never hit this; a hand-built plan can, and should hear
    about it in the engine's own vocabulary.
    """
    taken = {field.name for field in model._meta.get_fields()}
    taken |= {field.attname for field in model._meta.concrete_fields}
    for alias in plan.aliases:
        if alias in taken:
            raise errors.AliasCollisionError(
                f"Alias {alias!r} shadows a column on {model.__name__}"
            )


def _dimension_expression(registration: EntityRegistration, spec: DimensionSpec) -> Combinable:
    """Build the GROUP BY expression for one dimension."""
    field = registration.get_groupable(spec.field_path)
    assert_granularity_allowed(field, spec.granularity is not None)

    if spec.granularity is None:
        return F(field.field_path)

    # Bucketed in the caller's IANA zone, so one consistent wall clock is used
    # across a multi-region organization. ``DimensionSpec`` already refuses a
    # granularity without one.
    return Trunc(
        field.field_path,
        spec.granularity.value.lower(),
        output_field=DateTimeField(),
        tzinfo=spec.tzinfo,
    )


def _metric_expression(registration: EntityRegistration, spec: MetricSpec) -> Combinable:
    """Build the aggregate expression for one metric."""
    if spec.op is AggregateOp.COUNT:
        # Every group's row count. ``Count("id")`` rather than ``Count("*")``:
        # the primary key is NOT NULL, so the two agree, and naming a column
        # keeps the expression composable with ``distinct``.
        return Count("id", distinct=bool(spec.options.get("distinct", False)))

    if spec.op is AggregateOp.RELATION_COUNT:
        return _relation_count_expression(registration, spec)

    field = registration.get_aggregatable(spec.field_path)
    assert_op_supported(field, spec.op)

    if spec.op is AggregateOp.TRUE_COUNT:
        return Count("id", filter=Q(**{field.field_path: True}))
    if spec.op is AggregateOp.FALSE_COUNT:
        return Count("id", filter=Q(**{field.field_path: False}))

    source = field.build_source()

    if spec.op is AggregateOp.CONCAT:
        separator = str(spec.options.get("separator", ","))
        distinct = bool(spec.options.get("distinct", False))
        # Ordered by the aggregated value itself. Postgres requires the ORDER BY
        # of a DISTINCT aggregate to match its argument, and ordering by the
        # value is the only ordering that is deterministic for both variants.
        return StringAgg(source, Value(separator), distinct=distinct, order_by=source)

    if spec.op is AggregateOp.SUM:
        return Sum(source)
    if spec.op is AggregateOp.AVG:
        return Avg(source)
    if spec.op is AggregateOp.MIN:
        return Min(source)
    if spec.op is AggregateOp.MAX:
        return Max(source)

    raise errors.UnsupportedOperationError(f"Unhandled aggregate operation {spec.op.value!r}")


def _relation_count_expression(registration: EntityRegistration, spec: MetricSpec) -> Combinable:
    """Count a related collection without letting the join fan out.

    The count is a correlated scalar subquery per base row, summed over the
    group. Two of these in one query stay independent -- neither multiplies the
    other's rows, which is the bug that shows up the moment a second
    ``Count()`` over a different relation joins the same ``annotate()``.

    The subquery inherits the outer query's filters implicitly: it correlates
    on the base row's primary key, so only rows the base queryset selected
    contribute.
    """
    relation = registration.get_countable_relation(spec.field_path)
    rel = registration.model._meta.get_field(relation.relation_path)
    if not isinstance(rel, ForeignObjectRel) or rel.related_model is None:
        raise errors.UnknownFieldError(
            f"{relation.relation_path!r} on {registration.model.__name__} is not a "
            f"reverse relation and cannot be counted"
        )

    related_model: type[Model] = rel.related_model
    # ``_default_manager`` is the model's ``objects`` -- its
    # ``OrganizationScopedManager`` -- never ``_base_manager`` /
    # ``original_manager``. The assertion makes that a check rather than a
    # reading of the model file.
    assert_organization_scoped(related_model)

    # Traverse the organization-safe relation (``event__id``) rather than the
    # concrete ``event_fk_id`` column, so the join keeps its
    # organization-matched ``ON`` clause.
    forward_path = f"{rel.field.name}__id"
    related_count = (
        related_model._default_manager.filter(**{forward_path: OuterRef("pk")})
        .values(forward_path)
        .annotate(_related_count=Count("id"))
        .values("_related_count")
    )

    # The inner ``Coalesce`` turns "this row has no related rows" into 0 rather
    # than NULL; the outer one does the same for a group whose rows all have
    # none, which SUM would otherwise report as NULL.
    return Coalesce(
        Sum(Coalesce(Subquery(related_count, output_field=IntegerField()), Value(0))),
        Value(0),
        output_field=IntegerField(),
    )


def _ordering(plan: AggregateQueryPlan) -> tuple[str, ...]:
    """Return the ``order_by`` terms for the grouped queryset.

    A plan that names no ordering is ordered by its group key, so paging
    through ``offset``/``limit`` is stable. An unordered ``LIMIT`` returns an
    arbitrary slice of the groups, and the same request twice need not return
    the same one.
    """
    if plan.order_by:
        return tuple(order.as_order_by() for order in plan.order_by)
    return plan.dimension_aliases
