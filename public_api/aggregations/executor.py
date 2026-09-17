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
    ExpressionWrapper,
    F,
    FloatField,
    IntegerField,
    Max,
    Min,
    OuterRef,
    Q,
    QuerySet,
    RowRange,
    StringAgg,
    Subquery,
    Sum,
    Value,
    Window,
)
from django.db.models.expressions import Combinable, Func
from django.db.models.functions import Coalesce, Rank, TruncDay, TruncMonth, TruncWeek

from public_api.aggregations.errors import (
    AggregateRegistrationError,
    EntityQuerysetMismatchError,
    ReservedAliasError,
    UngroupedPartitionKeyError,
    UnknownHavingAliasError,
    UnknownWindowSourceError,
    UnsupportedAggregateOperationError,
)
from public_api.aggregations.plan import (
    FRAMED_WINDOW_FUNCTIONS,
    AggregateOp,
    AggregateQueryPlan,
    DimensionSpec,
    MetricSpec,
    WindowFunction,
    WindowMetricSpec,
    WindowSpec,
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
        # Every alias the predicate names has to be one this plan annotates.
        # An alias that is not resolves against the *model* instead, which
        # Django renders as a ``WHERE`` over a column -- dropping rows before
        # the grouping rather than groups after it. That is a plausible wrong
        # answer, so it is refused here rather than executed.
        _reject_unannotated_having_aliases(plan, set(group_by) | set(aggregates))
        # Applied after ``.annotate()``, which is what makes Django render it
        # as ``HAVING`` rather than folding it into the ``WHERE`` clause.
        grouped = grouped.filter(plan.having.predicate)

    if plan.window is not None:
        # Annotated *after* the ``having`` filter, and deliberately so: Django
        # refuses a filter over a window expression, and SQL computes window
        # functions after ``HAVING`` anyway. Doing it in this order means the
        # running total runs over the groups that survived the filter, which is
        # the only reading of "a window over the filtered result".
        window_annotations = _window_annotations(registration, plan.window, aggregates, group_by)
        for alias in window_annotations:
            _reject_reserved_alias(registration, alias, column_names)
        grouped = grouped.annotate(**window_annotations)

    grouped = grouped.order_by(*_order_by(plan, group_by))
    return grouped[plan.offset : plan.offset + plan.limit]


def execute_plan(plan: AggregateQueryPlan, queryset: QuerySet) -> list[dict[str, Any]]:
    """Run ``plan`` and return its rows, one dict per group.

    One database query. Rows arrive grouped and aggregated; nothing here
    reshapes them.
    """
    return list(build_aggregate_queryset(plan, queryset))


# ---------------------------------------------------------------------------
# Window functions
# ---------------------------------------------------------------------------
#
# The construction here is the one thing in this module worth reading in full
# before changing, because a wrong frame bound returns confident numbers.
#
# **Why the window can live in the same statement.** Postgres evaluates window
# functions *after* ``GROUP BY`` and ``HAVING``, so ``SUM(COUNT(id)) OVER
# (ORDER BY day)`` is a legal running total over grouped rows -- no subquery
# needed. Django refuses to build it only because ``Aggregate.resolve_expression``
# rejects an aggregate nested inside another aggregate, a rule that is right
# everywhere except inside an ``OVER`` clause. :class:`_WindowedSum` and
# :class:`_WindowedAvg` below opt out of exactly that check and change nothing
# else, which is what keeps the whole aggregate in one round trip instead of
# two. Everything is still ORM-generated; there is no raw SQL here.
#
# **Why the windows are annotated last.** ``Window.get_group_by_cols()`` returns
# nothing, so a window annotation never joins the ``GROUP BY`` list -- but
# Django *does* refuse a ``.filter()`` over one. Annotating after the ``having``
# filter keeps that impossible rather than merely unlikely.


class _GroupBlindWindow(Window):
    """A window that never contributes a column to ``GROUP BY``.

    ``Window.get_group_by_cols()`` reports the columns its ``PARTITION BY`` and
    ``ORDER BY`` read, so that a query inferring its grouping from scratch
    groups by them. This query does not infer anything: ``.values()`` already
    fixed the ``GROUP BY``, and every column a window here addresses is by
    construction one of those aliases -- ``partition_by`` is checked against
    them, and ``order_by`` names aliases the plan produces.

    Left to Django, that report is folded back in through
    ``Query.set_group_by()``, which -- depending on whether it decides aliases
    are usable, and that decision changes with the *other* annotations present
    -- can append the **window's own alias** to the ``GROUP BY``. Grouping by a
    window function is not something Postgres will run, and the trigger for it
    was a second window elsewhere in the same request. Reporting nothing is
    both correct here and the same answer every time.
    """

    def get_group_by_cols(self) -> list[Any]:
        return []


class _WindowedSum(Sum):
    """``SUM(<aggregate>)``, legal only inside an ``OVER`` clause.

    ``Aggregate.resolve_expression`` refuses an aggregate whose argument is
    itself an aggregate, which is correct for a bare ``SELECT`` and wrong for a
    window: ``SUM(COUNT(id)) OVER (...)`` is exactly how a running total over
    grouped rows is spelled. Resolving through ``Func`` skips that one check and
    inherits everything else.

    ``filter`` and ``default`` are refused rather than silently dropped --
    ``Aggregate.resolve_expression`` is where both are handled, and this does
    not run it.
    """

    def __init__(self, *expressions: Any, **extra: Any) -> None:
        if extra.get("filter") is not None or extra.get("default") is not None:
            raise AggregateRegistrationError(
                f"{type(self).__name__} does not support `filter` or `default`: they are "
                f"applied by the resolution path this class deliberately skips"
            )
        super().__init__(*expressions, **extra)

    def resolve_expression(
        self,
        query: Any = None,
        allow_joins: bool = True,
        reuse: Any = None,
        summarize: bool = False,
        for_save: bool = False,
    ) -> Any:
        return Func.resolve_expression(self, query, allow_joins, reuse, summarize, for_save)


class _WindowedAvg(Avg):
    """``AVG(<aggregate>)`` inside an ``OVER`` clause. See :class:`_WindowedSum`."""

    def __init__(self, *expressions: Any, **extra: Any) -> None:
        if extra.get("filter") is not None or extra.get("default") is not None:
            raise AggregateRegistrationError(
                f"{type(self).__name__} does not support `filter` or `default`: they are "
                f"applied by the resolution path this class deliberately skips"
            )
        super().__init__(*expressions, **extra)

    def resolve_expression(
        self,
        query: Any = None,
        allow_joins: bool = True,
        reuse: Any = None,
        summarize: bool = False,
        for_save: bool = False,
    ) -> Any:
        return Func.resolve_expression(self, query, allow_joins, reuse, summarize, for_save)


def _frame(spec: WindowSpec) -> RowRange:
    """The caller's frame as Django's ``RowRange``.

    ``None`` is Django's spelling of an unbounded end, and a ``PRECEDING``
    offset is negative while a ``FOLLOWING`` one is positive -- the plan's
    enums carry the direction and a non-negative row count, so the sign is
    applied here rather than asked of the caller.
    """
    return RowRange(
        start=_bound(spec.frame_start, spec.frame_start_offset),
        end=_bound(spec.frame_end, spec.frame_end_offset),
    )


def _bound(name: str, offset: int | None) -> int | None:
    """One frame bound as the integer ``RowRange`` wants.

    Both unbounded bounds map to ``None``, because that is the only thing
    Django accepts for either: it renders ``None`` as ``UNBOUNDED PRECEDING``
    at the start and ``UNBOUNDED FOLLOWING`` at the end, reading the *position*
    rather than the name. That mapping is only safe because
    :class:`~public_api.aggregations.plan.WindowSpec` has already refused an
    unbounded bound on the side it cannot occupy -- without that, this function
    would turn ``end: UNBOUNDED_PRECEDING`` into a whole-partition frame
    without a word.
    """
    if name == "CURRENT_ROW":
        return 0
    if name == "PRECEDING":
        return -(offset or 0)
    if name == "FOLLOWING":
        return offset or 0
    # UNBOUNDED_PRECEDING at the start, UNBOUNDED_FOLLOWING at the end -- and
    # `WindowSpec` guarantees it is never the other way round.
    return None


# A running total is cumulative by definition, so its frame is fixed here
# rather than read off the caller's -- see `FRAMED_WINDOW_FUNCTIONS`.
_RUNNING_FRAME = RowRange(start=None, end=0)


def _window_annotations(
    registration: EntityRegistration,
    spec: WindowSpec,
    aggregates: Mapping[str, Combinable],
    group_by: list[str],
) -> dict[str, Combinable]:
    """One annotation per requested window metric.

    ``aggregates`` is what the grouped queryset already annotates, keyed by row
    alias. A window metric names one of those aliases as its source and the
    expression is rebuilt from it here, so the running total is provably over
    the same number the row displays rather than over a second definition of it.

    Two orderings are built, and which one a metric gets depends on whether row
    *order* or only row *value* decides its answer. See ``_tiebroken`` below.
    """
    # A partition splits rows the query already grouped, so it can only address
    # a column those rows carry. Checked here as well as at the schema edge,
    # because the failure is a partition that silently widens the GROUP BY.
    for alias in spec.partition_by:
        if alias not in group_by:
            raise UngroupedPartitionKeyError(alias)

    partition_by = [F(alias) for alias in spec.partition_by]
    order_by = [
        F(order.alias).desc() if order.descending else F(order.alias).asc()
        for order in spec.order_by
    ]
    tiebroken_order_by = order_by + _tiebreak(spec, group_by)
    frame = _frame(spec)

    annotations: dict[str, Combinable] = {}
    for metric in spec.metrics:
        annotations[metric.alias] = _window_expression(
            metric, aggregates, partition_by, order_by, tiebroken_order_by, frame
        )
    return annotations


def _tiebreak(spec: WindowSpec, group_by: list[str]) -> list[Any]:
    """The group-key columns the caller's window ordering did not name.

    The mirror of what :func:`_order_by` does for the result ordering, and for
    the same reason: an ordering that does not fully determine row order leaves
    the rest to the database, and a running total accumulated in an arbitrary
    order over tied rows is a different series on the next run of the same
    query -- with a correct-looking final value and wrong intermediates.
    Appended ascending, in group-by order, so the sequence is the same every
    time.
    """
    named = {order.alias for order in spec.order_by}
    return [F(alias).asc() for alias in group_by if alias not in named]


def _window_expression(
    metric: WindowMetricSpec,
    aggregates: Mapping[str, Combinable],
    partition_by: list[Any],
    order_by: list[Any],
    tiebroken_order_by: list[Any],
    frame: RowRange,
) -> Combinable:
    """The ``OVER`` expression for one window metric."""
    if metric.function is WindowFunction.RANK:
        # Ranks rows by the window's own ordering, and gets the caller's
        # ordering *untiebroken*. A rank is decided by the ordering's values
        # rather than by the physical order of the rows carrying them, so it is
        # already the same on every run -- and appending a tiebreak would split
        # rows the caller asked to be tied, turning `RANK` into a row number and
        # contradicting the field's own "ties share a rank".
        return _GroupBlindWindow(Rank(), partition_by=partition_by or None, order_by=order_by)

    source = aggregates.get(metric.source_alias)
    if source is None:
        raise UnknownWindowSourceError(metric.alias, metric.source_alias)

    if metric.function is WindowFunction.PERCENT_OF_TOTAL:
        # This row's share of its partition. The denominator is the partition
        # total, so it takes no frame at all -- a framed denominator would make
        # the column a share of a moving window, which is not what it is named.
        total = _GroupBlindWindow(
            _WindowedSum(source), partition_by=partition_by or None, output_field=FloatField()
        )
        return ExpressionWrapper(
            source * Value(100.0) / total,
            output_field=FloatField(),
        )

    # Both of the remaining functions accumulate *across* rows, so which row
    # comes next changes their answer and they take the tiebroken ordering.
    applied_frame = frame if metric.function in FRAMED_WINDOW_FUNCTIONS else _RUNNING_FRAME
    if metric.function is WindowFunction.MOVING_AVG:
        return _GroupBlindWindow(
            _WindowedAvg(source),
            partition_by=partition_by or None,
            order_by=tiebroken_order_by,
            frame=applied_frame,
            output_field=FloatField(),
        )

    # RUNNING_SUM. A running count stays an integer; everything else is read as
    # a float, which is also what the GraphQL field publishes.
    return _GroupBlindWindow(
        _WindowedSum(source),
        partition_by=partition_by or None,
        order_by=tiebroken_order_by,
        frame=applied_frame,
        output_field=IntegerField() if _counts_rows(source) else FloatField(),
    )


def _counts_rows(source: Combinable) -> bool:
    """Whether this aggregate is a row count, whose running total is an integer.

    Asked of the resolved output field where there is one; an aggregate over an
    expression cannot always answer before resolution, and float is the right
    answer for every metric that is not a count.
    """
    return isinstance(getattr(source, "_output_field_or_none", None), IntegerField)


def _having_aliases(predicate: Q) -> set[str]:
    """Every row key a ``HAVING`` predicate reads.

    A ``Q``'s leaves are ``(lookup, value)`` pairs whose lookup is an alias
    followed by zero or more ``__``-separated lookups, so the alias is the
    first segment. Nested ``Q``s -- what ``and`` / ``or`` composition builds --
    are walked through.
    """
    aliases: set[str] = set()
    for child in predicate.children:
        if isinstance(child, Q):
            aliases |= _having_aliases(child)
            continue
        lookup = child[0] if isinstance(child, tuple) else str(child)
        aliases.add(lookup.split("__", 1)[0])
    return aliases


def _reject_unannotated_having_aliases(plan: AggregateQueryPlan, annotated: set[str]) -> None:
    """Refuse a ``HAVING`` naming a row key this plan does not produce.

    The layer that builds the predicate also decides the plan's metrics, so it
    is that layer's job to add a metric the ``having`` referenced but the
    selection did not -- see
    :func:`public_api.aggregations.having.having_from_input`, which returns
    both halves together. This is the check that the two agreed.
    """
    if plan.having is None:
        return
    for alias in sorted(_having_aliases(plan.having.predicate) - annotated):
        raise UnknownHavingAliasError(alias)


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
