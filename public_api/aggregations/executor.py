"""Turn an :class:`AggregateQueryPlan` into an ORM queryset.

The whole job is one ``.values(...).annotate(...)`` over a base queryset the
caller supplies. Three rules shape it.

**Tenant scoping is not this module's to do.** The base queryset arrives
already scoped -- from the model's ``OrganizationScopedManager`` and the
``public_api.scoping`` helpers -- and the relation-count subqueries below go
through ``objects`` for the same reason. Nothing here calls ``unscoped()`` or
``original_manager``. A ``GROUP BY`` over an unscoped queryset returns
plausible numbers that quietly include another organization's rows, which is
the one bug in this feature that would not look like a bug in its output.

**Multi-relation counts are subqueries, not joins.** Two ``Count()`` calls
over different relations in one ``annotate()`` produce a cross join between
the two relations, and each count comes back multiplied by the other's row
count. ``public_api/queries.py``'s ``childOrganizations`` already solved that
with ``Subquery``-wrapped per-relation counts; this reuses the shape rather
than rediscovering the bug. The subquery is annotated on the base queryset,
*before* grouping, and summed afterwards -- annotating it after ``.values()``
makes Django add the subquery itself to the ``GROUP BY``, which splits groups
on the count instead of aggregating them.

**Temporal dimensions truncate in the caller's timezone.** A dimension
carrying a granularity becomes ``DATE_TRUNC(<width>, col AT TIME ZONE <tz>)``
and groups on that, so one wall clock measures every bucket in the result
regardless of what timezone each row stores. Buckets are sparse: the query
groups the rows that exist, so a day with none produces no row at all rather
than a zero.

**A feature this phase does not build is refused, never ignored.**
``HAVING`` (Phase 4) and window functions (Phase 6) each raise here until
their phase lands. A window clause that is silently dropped returns
confident numbers that are not the ones asked for.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from django.db.models import (
    Avg,
    Count,
    F,
    IntegerField,
    Max,
    Min,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    Value,
)
from django.db.models.aggregates import StringAgg
from django.db.models.expressions import BaseExpression, Combinable
from django.db.models.functions import Coalesce, TruncDay, TruncMonth, TruncWeek
from django.db.models.functions.datetime import TruncBase

from public_api.aggregations.errors import (
    AliasCollisionError,
    InvalidPlanError,
    NonTemporalGranularityError,
    QuerysetModelMismatchError,
    UnsupportedOperationError,
    UnsupportedPlanFeatureError,
)
from public_api.aggregations.plan import (
    ROW_COUNT_FIELD_PATH,
    AggregateOp,
    AggregateQueryPlan,
    DimensionSpec,
    MetricSpec,
    OrderDirection,
)
from public_api.aggregations.registry import EntityRegistration, RelationCount, get_registration
from public_api.aggregations.types import TemporalGranularity


#: Prefix for the per-row relation-count annotations the executor adds to the
#: base queryset before grouping. Nothing constrains a caller's alias to avoid
#: it -- a metric aliased ``_relation_count_x`` would collide -- but Django
#: refuses a duplicate annotation name outright, so the collision is a loud
#: error rather than a wrong number, and the prefix makes it implausible.
_RELATION_COUNT_PREFIX = "_relation_count_"


def build_aggregate_queryset(
    plan: AggregateQueryPlan, base_queryset: QuerySet[Any]
) -> QuerySet[Any, dict[str, Any]]:
    """Build the grouped, annotated queryset ``plan`` describes.

    ``base_queryset`` is the already-scoped and already-filtered set of rows
    to aggregate. It must be over the model the plan's entity is registered
    with; a mismatch is refused rather than silently aggregating the wrong
    table.

    Returns a lazy queryset of row dicts, one per non-empty group, keyed by
    the plan's dimension and metric aliases. Nothing is evaluated here.
    """
    registration = get_registration(plan.entity)
    if base_queryset.model is not registration.model:
        raise QuerysetModelMismatchError(registration.model, base_queryset.model)

    _reject_unbuilt_features(plan)

    model_field_names = _model_field_names(registration)
    _reject_shadowing_aliases(registration, plan, model_field_names)

    queryset = base_queryset
    relation_counts = _relation_count_metrics(registration, plan)
    if relation_counts:
        queryset = queryset.annotate(
            **{
                _RELATION_COUNT_PREFIX + alias: _relation_count_subquery(relation)
                for alias, relation in relation_counts.items()
            }
        )

    values_args, values_kwargs = _group_by_terms(registration, plan)
    queryset = queryset.values(*values_args, **values_kwargs)

    queryset = queryset.annotate(
        **{
            metric.alias: _metric_expression(registration, metric, relation_counts)
            for metric in plan.metrics
        }
    )

    # Always explicit, for two reasons: it clears any model-level default
    # ordering, which would otherwise add its columns to the GROUP BY and
    # split groups; and paging over an unordered grouped query returns
    # arbitrary rows per page.
    queryset = queryset.order_by(*_ordering_terms(plan))

    return queryset[plan.offset : plan.offset + plan.limit]


def _reject_unbuilt_features(plan: AggregateQueryPlan) -> None:
    """Refuse the parts of a plan later phases of the engine own."""
    if plan.having is not None:
        raise UnsupportedPlanFeatureError(
            "HAVING is not built yet; it lands with the having and metric-ordering phase"
        )
    if plan.window is not None:
        raise UnsupportedPlanFeatureError(
            "Window functions are not built yet; they land with the window-function phase"
        )


def _model_field_names(registration: EntityRegistration) -> frozenset[str]:
    """Every name Django will treat as a column on the grouped model.

    Both ``name`` and ``attname`` (``calendar`` and ``calendar_fk_id``),
    because Django's own conflict check in ``QuerySet._annotate`` looks at
    both -- an alias matching either raises ``ValueError``.
    """
    names: set[str] = set()
    for model_field in registration.model._meta.get_fields():
        names.add(model_field.name)
        attname = getattr(model_field, "attname", None)
        if attname is not None:
            names.add(attname)
    return frozenset(names)


def _reject_shadowing_aliases(
    registration: EntityRegistration, plan: AggregateQueryPlan, model_field_names: frozenset[str]
) -> None:
    """Refuse an alias that Django would reject as shadowing a column.

    Django raises a bare ``ValueError`` on an annotation whose name is a
    model field; this raises first, naming the alias and the model.

    The two halves of a plan get different rules because they reach the
    queryset differently. A *dimension* whose alias is its own column name is
    passed positionally to ``.values()`` and never annotated, so that one
    case is fine -- any other shadowing alias is not. A *metric* is always
    annotated, so ``MIN(title)`` cannot be called ``title``: it needs an
    alias of its own, ``title_min``.
    """
    for dimension in plan.dimensions:
        if _is_pass_through(dimension):
            continue
        if dimension.alias in model_field_names:
            raise AliasCollisionError(
                f"Dimension alias {dimension.alias!r} shadows a column on "
                f"{registration.model.__name__}"
            )

    for metric in plan.metrics:
        if metric.alias in model_field_names:
            raise AliasCollisionError(
                f"Metric alias {metric.alias!r} shadows a column on "
                f"{registration.model.__name__}; give the aggregate its own alias"
            )


def _group_by_terms(
    registration: EntityRegistration, plan: AggregateQueryPlan
) -> tuple[tuple[str, ...], dict[str, Combinable]]:
    """Split the dimensions into positional and aliased ``.values()`` terms.

    A dimension whose alias already *is* its column name is passed
    positionally: ``.values(timezone=F("timezone"))`` is rejected by Django
    as an annotation that conflicts with a model field.
    """
    positional: list[str] = []
    aliased: dict[str, Combinable] = {}

    for dimension in plan.dimensions:
        registered = registration.dimension(dimension.field_path)
        if dimension.granularity is not None and not registered.temporal:
            raise NonTemporalGranularityError(
                f"{dimension.field_path!r} is not a temporal dimension of "
                f"{registration.model.__name__} and cannot carry a granularity"
            )
        if _is_pass_through(dimension):
            positional.append(dimension.field_path)
        else:
            aliased[dimension.alias] = _dimension_expression(dimension)

    return tuple(positional), aliased


def _is_pass_through(dimension: DimensionSpec) -> bool:
    """Whether this dimension can go straight into ``.values()`` by name.

    Only when it is untruncated *and* already named after its column. A
    bucketed dimension is always an expression, so it is always aliased --
    passing ``start_time`` positionally because its alias happened to match
    would drop the truncation and group on the raw timestamp, which is one
    group per row.
    """
    return dimension.granularity is None and dimension.alias == dimension.field_path


#: Granularity to the ORM function that truncates to it. ``TruncWeek`` starts
#: weeks on Monday, which is Django's own convention and the one
#: ``TemporalGranularity.WEEK`` documents.
_TRUNC_BY_GRANULARITY: Mapping[TemporalGranularity, type[TruncBase]] = MappingProxyType(
    {
        TemporalGranularity.DAY: TruncDay,
        TemporalGranularity.WEEK: TruncWeek,
        TemporalGranularity.MONTH: TruncMonth,
    }
)


def _dimension_expression(dimension: DimensionSpec) -> Combinable:
    """The grouped expression for one dimension.

    Untruncated dimensions pass their column through. A bucketed one becomes
    ``DATE_TRUNC(<width>, col AT TIME ZONE <tz>)``: the timezone is the
    caller's, taken from the plan rather than from Django's ambient
    ``TIME_ZONE``, so the buckets do not silently follow the server's idea of
    a day.
    """
    if dimension.granularity is None:
        return F(dimension.field_path)
    trunc = _TRUNC_BY_GRANULARITY[dimension.granularity]
    return trunc(dimension.field_path, tzinfo=dimension.tzinfo)


def _relation_count_metrics(
    registration: EntityRegistration, plan: AggregateQueryPlan
) -> dict[str, RelationCount]:
    """The plan's relation-count metrics, keyed by the alias each lands under."""
    matched: dict[str, RelationCount] = {}
    for metric in plan.metrics:
        relation = registration.relation_counts.get(metric.field_path)
        if relation is None:
            continue
        if metric.op is not AggregateOp.COUNT:
            raise UnsupportedOperationError(metric.op, metric.field_path, "relation")
        matched[metric.alias] = relation
    return matched


def _relation_count_subquery(relation: RelationCount) -> Combinable:
    """A per-row count of ``relation``'s rows, as a correlated subquery.

    ``objects`` rather than ``original_manager``: the related model's default
    manager is what puts the bound organization into the subquery's ``WHERE``
    clause, and for ``BlockedTime`` / ``AvailableTime`` it is also what keeps
    appointment-type-scoped rows out of a count of base rows.

    ``Coalesce`` to zero because a parent with no related rows matches no
    subquery row at all, and ``NULL`` is not the answer to "how many".
    """
    # ``objects`` is not declared on django-stubs' ``type[Model]``; every model
    # registered here declares one, and it is the scoped manager this needs.
    manager = relation.model.objects  # type: ignore[attr-defined]
    inner = (
        manager.filter(**{relation.relation_column: OuterRef("pk")})
        .values(relation.relation_column)
        .annotate(relation_count=Count("id"))
        .values("relation_count")
    )
    return Coalesce(Subquery(inner, output_field=IntegerField()), Value(0))


def _metric_expression(
    registration: EntityRegistration,
    metric: MetricSpec,
    relation_counts: dict[str, RelationCount],
) -> BaseExpression:
    """The aggregate expression for one metric."""
    if metric.alias in relation_counts:
        # Summed, not re-counted: the per-row subquery already holds each
        # grouped row's related total, so the group's total is their sum.
        # Grouping by the entity's own id makes that a sum of one.
        return Sum(_RELATION_COUNT_PREFIX + metric.alias)

    if metric.op is AggregateOp.COUNT:
        if metric.field_path != ROW_COUNT_FIELD_PATH:
            raise InvalidPlanError(
                f"COUNT over {metric.field_path!r} is neither the row count "
                f"({ROW_COUNT_FIELD_PATH!r}) nor a registered relation count"
            )
        return Count(ROW_COUNT_FIELD_PATH, distinct=bool(metric.options.get("distinct", False)))

    aggregatable = registration.checked_metric(metric.field_path, metric.op)
    target: str | Combinable = (
        aggregatable.expression_factory()
        if aggregatable.expression_factory is not None
        else aggregatable.field_path
    )

    match metric.op:
        case AggregateOp.SUM:
            return Sum(target)
        case AggregateOp.AVG:
            return Avg(target)
        case AggregateOp.MIN:
            return Min(target)
        case AggregateOp.MAX:
            return Max(target)
        case AggregateOp.CONCAT:
            # ``order_by`` on the aggregated value itself, which is what makes
            # the result reproducible: Postgres leaves an unordered
            # ``string_agg`` in whatever order the rows happened to arrive, so
            # the same group can concatenate differently between runs. Ordering
            # by the same expression the aggregate is over is also the form
            # Postgres requires alongside ``DISTINCT``.
            return StringAgg(
                target,
                delimiter=Value(str(metric.options.get("separator", ","))),
                distinct=bool(metric.options.get("distinct", False)),
                order_by=target,
            )
        case AggregateOp.TRUE_COUNT:
            return Count(ROW_COUNT_FIELD_PATH, filter=Q(**{aggregatable.field_path: True}))
        case AggregateOp.FALSE_COUNT:
            return Count(ROW_COUNT_FIELD_PATH, filter=Q(**{aggregatable.field_path: False}))

    raise UnsupportedOperationError(metric.op, metric.field_path, aggregatable.kind)


def _ordering_terms(plan: AggregateQueryPlan) -> tuple[str, ...]:
    """The ``.order_by()`` terms, with the group key appended as a tiebreak.

    Two groups that tie on the requested ordering would otherwise come back
    in whatever order Postgres chose that run, which makes paging drop and
    repeat rows between pages.
    """
    requested = tuple(
        f"-{order.alias}" if order.direction is OrderDirection.DESC else order.alias
        for order in plan.order_by
    )
    ordered_aliases = {order.alias for order in plan.order_by}
    tiebreak = tuple(alias for alias in plan.dimension_aliases if alias not in ordered_aliases)
    return requested + tiebreak
