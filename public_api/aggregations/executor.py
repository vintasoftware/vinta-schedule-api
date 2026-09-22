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

**Window functions ride in the same statement as the grouping.** Postgres
evaluates an ``OVER`` clause after ``GROUP BY`` and after ``HAVING``, so a
running total over grouped rows is ``SUM(COUNT("id")) OVER (...)`` at this one
query level -- no second query, no subquery, nothing accumulated in Python.
Django will not build that through ``Sum(Count(...))``; :class:`WindowAggregate`
and :class:`GroupedWindow` below are what make the ORM say it, and each
carries the reason it exists. ``LIMIT`` is applied after the windows are
computed, which is what lets a top-N page carry a running total over the whole
result rather than over the page.

**HAVING is a ``.filter()`` after the ``.annotate()``, nothing more.**
Django renders a post-``GROUP BY`` ``.filter()`` referencing an aggregate
alias as SQL ``HAVING`` on its own; this module only has to turn a
:class:`~public_api.aggregations.plan.HavingSpec` tree into the matching
``Q()`` tree. Every alias a ``HavingSpec`` leaf names is guaranteed to
already be one of ``plan.metrics`` or ``plan.dimensions`` -- the resolver
layer (``public_api.aggregations.having``) is what adds a metric HAVING
references but the row selection did not, before the plan is ever built --
so this module never needs to annotate anything on ``HavingSpec``'s behalf.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from django.db.models import (
    Avg,
    Count,
    ExpressionWrapper,
    F,
    FloatField,
    Func,
    IntegerField,
    Max,
    Min,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    Value,
    Window,
)
from django.db.models.aggregates import StringAgg
from django.db.models.expressions import BaseExpression, Combinable, OrderBy, RowRange, ValueRange
from django.db.models.expressions import WindowFrame as DjangoWindowFrame
from django.db.models.functions import Coalesce, NullIf, Rank, TruncDay, TruncMonth, TruncWeek
from django.db.models.functions.datetime import TruncBase

from public_api.aggregations.errors import (
    AliasCollisionError,
    InvalidPlanError,
    NonTemporalGranularityError,
    QuerysetModelMismatchError,
    UnsupportedOperationError,
)
from public_api.aggregations.plan import (
    ROW_COUNT_FIELD_PATH,
    AggregateOp,
    AggregateQueryPlan,
    ComparisonOp,
    DimensionSpec,
    HavingSpec,
    MetricSpec,
    OrderDirection,
    WindowFrameSpec,
    WindowFrameType,
    WindowFunctionKind,
    WindowSpec,
    window_alias,
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

    if plan.having is not None:
        queryset = queryset.filter(_having_q(plan.having))

    # After the HAVING, because Postgres computes a window over the groups
    # that survived it -- a running total counting groups the caller filtered
    # out would not agree with the rows printed beside it.
    window_annotations = _window_annotations(plan)
    if window_annotations:
        queryset = queryset.annotate(**window_annotations)

    # Always explicit, for two reasons: it clears any model-level default
    # ordering, which would otherwise add its columns to the GROUP BY and
    # split groups; and paging over an unordered grouped query returns
    # arbitrary rows per page.
    queryset = queryset.order_by(*_ordering_terms(plan))

    return queryset[plan.offset : plan.offset + plan.limit]


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


# ---------------------------------------------------------------------------
# Window functions over the grouped rows
# ---------------------------------------------------------------------------


class WindowAggregate(Func):
    """A plain SQL function call that ``Window`` accepts as its expression.

    The point is to say ``SUM(COUNT("id")) OVER (...)``, which is what a
    running total over a grouped result *is* in Postgres: window functions are
    evaluated after ``GROUP BY`` and after ``HAVING``, so the thing being
    summed is the group's own aggregate. Django refuses to build that through
    ``Sum(Count(...))`` -- ``Aggregate.resolve_expression`` raises "Cannot
    compute Sum('Count'): 'Count' is an aggregate" -- because an aggregate of
    an aggregate is meaningless anywhere except inside an ``OVER`` clause.

    ``Func`` carries no such guard, and ``window_compatible`` is the whole of
    what ``Window`` asks of its expression. This stays inside the ORM's public
    expression classes: no raw SQL, and the function name is one of this
    module's own two constants rather than anything a caller supplies.
    """

    window_compatible = True


class GroupedWindow(Window):
    """A ``Window`` that is never itself a ``GROUP BY`` term.

    Django's ``Window.get_group_by_cols`` returns its partition and ordering
    columns, and ``Query.set_group_by`` turns a non-empty result for a
    non-aggregate annotation into ``GROUP BY <that annotation>``. Adding an
    annotation that *does* contain an aggregate -- ``percentOfTotal`` divides
    the row's own metric by a window -- re-runs ``set_group_by``, and every
    window annotation then lands in the ``GROUP BY``, which Postgres rejects
    outright.

    Returning nothing is the truth rather than a workaround: a window is
    computed after grouping, so it can never be a grouping key, and the
    columns it reads are already group keys -- ``partition_by`` is validated
    against the plan's own dimensions, and the ordering names either a
    dimension or an annotated metric.
    """

    def get_group_by_cols(self) -> list[BaseExpression]:
        return []


class WindowRatio(ExpressionWrapper):
    """Arithmetic around an ``OVER`` clause, for the same reason as
    :class:`GroupedWindow`: it reads a window, so it belongs after the
    grouping rather than in it."""

    def get_group_by_cols(self) -> list[BaseExpression]:
        return []


#: The frame a running total always uses, and the one a moving average falls
#: back to: the whole partition up to and including the current row.
_DEFAULT_WINDOW_FRAME = WindowFrameSpec()

#: The two SQL aggregates a window may apply to its metric. Named here so the
#: string that reaches ``Func(function=...)`` is always one of these two.
_WINDOW_SUM = "SUM"
_WINDOW_AVG = "AVG"


def _window_metric(spec: WindowSpec, function: str) -> WindowAggregate:
    """``SUM`` or ``AVG`` over the one metric this window reads.

    ``F(alias)`` resolves to the metric's own aggregate expression, which is
    what puts the aggregate inside the function call rather than beside it.
    """
    return WindowAggregate(F(spec.metric_alias), function=function, output_field=FloatField())


def _window_partition(spec: WindowSpec) -> list[F] | None:
    """The ``PARTITION BY`` terms, or ``None`` for one partition over the whole
    result -- which is what ``Window`` reads as "no PARTITION BY clause"."""
    return [F(alias) for alias in spec.partition_by] or None


def _window_ordering(spec: WindowSpec) -> list[OrderBy]:
    """The window's own ``ORDER BY``, which is not the result's."""
    return [
        F(order.alias).desc() if order.direction is OrderDirection.DESC else F(order.alias).asc()
        for order in spec.order_by
    ]


def _window_frame(frame: WindowFrameSpec) -> DjangoWindowFrame:
    """One frame clause, counted in rows or in peers as the caller asked."""
    start, end = frame.bounds()
    frame_class = ValueRange if frame.frame_type is WindowFrameType.RANGE else RowRange
    return frame_class(start=start, end=end)


def _window_expression(spec: WindowSpec, kind: WindowFunctionKind) -> BaseExpression:
    """The ORM expression one window function becomes."""
    partition = _window_partition(spec)
    ordering = _window_ordering(spec)

    match kind:
        case WindowFunctionKind.RUNNING_TOTAL:
            # Always cumulative from the start of the partition, and
            # deliberately not the caller's frame: a running total that read
            # only the last three rows would be a rolling sum wearing the
            # wrong name, and one query can carry this *and* a framed moving
            # average only if the two do not share a frame.
            return GroupedWindow(
                _window_metric(spec, _WINDOW_SUM),
                partition_by=partition,
                order_by=ordering,
                frame=_window_frame(_DEFAULT_WINDOW_FRAME),
            )
        case WindowFunctionKind.MOVING_AVERAGE:
            # The one function the frame shapes. Without one it averages the
            # same rows the running total sums, which is a cumulative average.
            return GroupedWindow(
                _window_metric(spec, _WINDOW_AVG),
                partition_by=partition,
                order_by=ordering,
                frame=_window_frame(spec.frame or _DEFAULT_WINDOW_FRAME),
            )
        case WindowFunctionKind.RANK:
            # No frame: a rank is a position in the ordering, and a frame would
            # describe rows it does not read. Postgres refuses one here anyway.
            return GroupedWindow(Rank(), partition_by=partition, order_by=ordering)
        case WindowFunctionKind.PERCENT_OF_TOTAL:
            # Neither ordering nor frame, deliberately: either would turn this
            # into a share of the rows so far, which does not sum to 100 across
            # the partition. ``NULLIF`` keeps a partition whose metric sums to
            # zero from aborting the statement on a division by zero -- the
            # share of nothing is not a number, and ``null`` says so.
            partition_total = GroupedWindow(
                _window_metric(spec, _WINDOW_SUM), partition_by=partition
            )
            return WindowRatio(
                Value(100.0) * F(spec.metric_alias) / NullIf(partition_total, Value(0.0)),
                output_field=FloatField(),
            )

    raise InvalidPlanError(f"Unknown window function {kind!r}")  # pragma: no cover


def _window_annotations(plan: AggregateQueryPlan) -> dict[str, BaseExpression]:
    """Every window column the plan asks for, keyed by the alias it lands under.

    Empty when the plan has no window, and also when it has one whose
    functions are empty -- a ``window`` argument the caller never read any
    function out of is still validated, but there is nothing to compute for
    it.
    """
    spec = plan.window
    if spec is None or not spec.functions:
        return {}
    return {window_alias(kind): _window_expression(spec, kind) for kind in spec.functions}


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


#: ``ComparisonOp`` to the Django field lookup it filters an annotated alias
#: with. ``NE`` is absent: it is a negated ``exact``, built separately in
#: :func:`_having_q` rather than forced into this table as a fake lookup name.
_COMPARISON_LOOKUP: Mapping[ComparisonOp, str] = MappingProxyType(
    {
        ComparisonOp.EQ: "exact",
        ComparisonOp.GT: "gt",
        ComparisonOp.GTE: "gte",
        ComparisonOp.LT: "lt",
        ComparisonOp.LTE: "lte",
    }
)


def _having_q(node: HavingSpec) -> Q:
    """One ``HavingSpec`` node, as the ``Q()`` Django filters the annotated
    queryset with. Every alias a leaf names is already one of ``plan.metrics``
    or ``plan.dimensions`` by the time a plan reaches here -- see the
    resolver layer's ``having.py`` -- so this never has to annotate anything
    itself; it only has to walk the tree.
    """
    if node.comparison is not None:
        comparison = node.comparison
        if comparison.comparison is ComparisonOp.NE:
            return ~Q(**{comparison.alias: comparison.value})
        lookup = _COMPARISON_LOOKUP[comparison.comparison]
        return Q(**{f"{comparison.alias}__{lookup}": comparison.value})

    if node.all_of:
        combined = Q()
        for child in node.all_of:
            combined &= _having_q(child)
        return combined

    combined = Q()
    for index, child in enumerate(node.any_of):
        combined = _having_q(child) if index == 0 else combined | _having_q(child)
    return combined
