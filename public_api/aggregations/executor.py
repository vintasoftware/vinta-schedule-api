"""Turns an ``AggregateQueryPlan`` into one grouped ORM query.

The whole aggregate is one ``.values(...).annotate(...)`` over a base queryset
the caller supplies, and the rows come back already grouped and already
aggregated. Nothing here sums, sorts, buckets or synthesises a row in Python.

**Tenant scoping is the caller's queryset, not this module's business.** The
base queryset arrives from the model's own ``OrganizationScopedManager`` (or
from one of the ``public_api/scoping.py`` helpers narrowed on top of it), and
nothing here calls ``original_manager`` or ``unscoped()``. A ``GROUP BY`` over
an unscoped queryset would return plausible numbers that silently include
another organization's rows — the one bug in this feature that would not look
like a bug in its output. The relation-count subqueries below go through the
related model's ``objects`` for the same reason.
"""

from collections.abc import Mapping
from typing import Any

from django.db import models
from django.db.models.aggregates import StringAgg
from django.db.models.functions import Coalesce

from public_api.aggregations.errors import (
    ENTITY_MISMATCH_MESSAGE,
    LIMIT_OUT_OF_RANGE_MESSAGE,
    OFFSET_NEGATIVE_MESSAGE,
    AggregateLimitError,
    InvalidAggregatePlanError,
    UnknownAggregateFieldError,
    UnsupportedAggregateOperationError,
    unknown_aggregate_field_message,
    unknown_group_by_field_message,
    unsupported_operation_message,
)
from public_api.aggregations.plan import (
    AggregateOp,
    AggregateQueryPlan,
    DimensionSpec,
    MetricSpec,
)
from public_api.aggregations.registry import (
    COUNT_METRIC_NAME,
    AggregatableField,
    EntityRegistration,
    RelationCountField,
    get_registration,
)


MAX_AGGREGATE_LIMIT = 100

#: What a dimension or a metric resolves to: a plain column reference, or a
#: derived expression such as ``duration_minutes``.
type OrmExpression = models.F | models.Expression

#: Default separator for ``CONCAT``, matching the ``concat`` field's GraphQL default.
DEFAULT_CONCAT_SEPARATOR = ","


def _check_slice(plan: AggregateQueryPlan) -> None:
    """Refuse a limit or offset outside the bounds every list field here uses."""
    if plan.offset < 0:
        raise AggregateLimitError(OFFSET_NEGATIVE_MESSAGE)
    if plan.limit <= 0 or plan.limit > MAX_AGGREGATE_LIMIT:
        raise AggregateLimitError(LIMIT_OUT_OF_RANGE_MESSAGE)


def _reject_unbuilt_features(plan: AggregateQueryPlan) -> None:
    """Refuse plan features this phase's executor does not build yet.

    ``granularity`` arrives with Phase 2, ``having`` with Phase 4 and ``window``
    with Phase 6. Each of them changes the answer, so dropping one silently
    would return a confidently wrong number; raising keeps the failure loud
    until the phase that implements it deletes the matching check. Nothing
    reaches this module from the GraphQL schema yet, so no caller can trip it.
    """
    if any(dimension.granularity is not None for dimension in plan.dimensions):
        raise NotImplementedError("Temporal bucketing lands with Phase 2 of this plan.")
    if plan.having is not None:
        raise NotImplementedError("HAVING lands with Phase 4 of this plan.")
    if plan.window is not None:
        raise NotImplementedError("Window functions land with Phase 6 of this plan.")


def _dimension_target(dimension: DimensionSpec) -> tuple[str, OrmExpression | None]:
    """Return what ``.values()`` should be given for this dimension.

    A dimension whose alias is already the ORM path is passed positionally:
    ``values(calendar_fk_id=F("calendar_fk_id"))`` is rejected by Django as
    conflicting with a model field. Anything else is an aliased expression.
    """
    if dimension.alias == dimension.field_path:
        return dimension.alias, None
    return dimension.alias, models.F(dimension.field_path)


def _metric_source(registered: AggregatableField) -> OrmExpression:
    """The thing an aggregate function is applied to — a column or a derived expression."""
    if registered.expression is not None:
        return registered.expression()
    return models.F(registered.field_path)


def _relation_count_expression(relation: RelationCountField) -> models.Expression:
    """Count related rows per parent, then sum those counts across the group.

    Counting through the join instead (``Count("events__id")``) multiplies every
    other relation's count by this one's fan-out. ``public_api/queries.py``
    already solves this for ``childOrganizations`` with per-relation
    ``Subquery`` counts; this is the same shape with a ``Sum`` on top, because
    an aggregate row covers many parents rather than one.

    ``objects`` — not ``original_manager`` — so the subquery is organization
    scoped like everything else in the statement.
    """
    per_parent = (
        relation.related_model.objects.filter(  # type: ignore[attr-defined]
            **{relation.parent_field_path: models.OuterRef("pk")}
        )
        .values(relation.parent_field_path)
        .annotate(related_row_count=models.Count("id"))
        .values("related_row_count")
    )
    return models.Sum(
        Coalesce(
            models.Subquery(per_parent, output_field=models.IntegerField()),
            models.Value(0),
            output_field=models.IntegerField(),
        ),
        output_field=models.IntegerField(),
    )


def _concat_expression(
    registered: AggregatableField, options: Mapping[str, Any]
) -> models.Expression:
    """``string_agg`` over a text column, ordered so repeated runs agree.

    Postgres does not promise an order for ``string_agg`` without one, so two
    runs of the same query could return the same values in a different string.
    Ordering by the aggregated column itself makes the result reproducible.
    """
    separator = options.get("separator", DEFAULT_CONCAT_SEPARATOR)
    distinct = bool(options.get("distinct", False))
    source = _metric_source(registered)
    return StringAgg(
        source,
        models.Value(separator),
        distinct=distinct,
        order_by=source,
    )


def _metric_expression(metric: MetricSpec, registration: EntityRegistration) -> models.Expression:
    """Build the annotation for one metric, validating it against the registry."""
    if metric.op is AggregateOp.COUNT:
        relation = registration.relation_counts.get(metric.field_path)
        if relation is not None:
            return _relation_count_expression(relation)
        if metric.field_path == COUNT_METRIC_NAME:
            # The group's own row count. ``Count("id")`` and never
            # ``Count("*")``: a primary key is NOT NULL, so the two agree, and
            # naming the column keeps the aggregate composable with a filter.
            return models.Count("id")
        raise UnknownAggregateFieldError(unknown_aggregate_field_message(metric.field_path))

    registered = registration.assert_operation_supported(metric.field_path, metric.op)
    source = _metric_source(registered)

    match metric.op:
        case AggregateOp.SUM:
            return models.Sum(source)
        case AggregateOp.AVG:
            return models.Avg(source)
        case AggregateOp.MIN:
            return models.Min(source)
        case AggregateOp.MAX:
            return models.Max(source)
        case AggregateOp.CONCAT:
            return _concat_expression(registered, metric.options)
        case AggregateOp.TRUE_COUNT:
            return models.Count("id", filter=models.Q(**{registered.field_path: True}))
        case AggregateOp.FALSE_COUNT:
            return models.Count("id", filter=models.Q(**{registered.field_path: False}))

    raise UnsupportedAggregateOperationError(
        unsupported_operation_message(metric.field_path, metric.op.value)
    )


def build_aggregate_queryset(
    plan: AggregateQueryPlan, queryset: models.QuerySet
) -> models.QuerySet:
    """Return the grouped queryset for a plan, without evaluating it.

    ``queryset`` is the already-scoped, already-filtered base rows. This adds
    the ``GROUP BY``, the aggregates, a deterministic ordering and the slice —
    and nothing else.
    """
    registration = get_registration(plan.entity)
    if queryset.model is not registration.model:
        raise InvalidAggregatePlanError(ENTITY_MISMATCH_MESSAGE)

    _check_slice(plan)
    _reject_unbuilt_features(plan)

    # Whoever built the plan, the dimensions still have to be ones the entity
    # offers: a path that is not registered would otherwise reach ``.values()``
    # and group on a column nobody decided was groupable.
    groupable_paths = {registered.field_path for registered in registration.groupable.values()}

    plain_dimensions: list[str] = []
    aliased_dimensions: dict[str, OrmExpression] = {}
    for dimension in plan.dimensions:
        if dimension.field_path not in groupable_paths:
            raise UnknownAggregateFieldError(unknown_group_by_field_message(dimension.field_path))
        alias, expression = _dimension_target(dimension)
        if expression is None:
            plain_dimensions.append(alias)
        else:
            aliased_dimensions[alias] = expression

    metrics = {metric.alias: _metric_expression(metric, registration) for metric in plan.metrics}

    grouped = queryset.values(*plain_dimensions, **aliased_dimensions).annotate(**metrics)
    return grouped.order_by(*_ordering(plan))[plan.offset : plan.offset + plan.limit]


def _ordering(plan: AggregateQueryPlan) -> tuple[str, ...]:
    """Deterministic ordering for the grouped rows.

    Explicit ``order_by`` wins. Otherwise the group key orders the result, so a
    caller paging with ``offset`` sees each group exactly once — an unordered
    ``LIMIT``/``OFFSET`` over a ``GROUP BY`` is free to return a different
    permutation on every page.
    """
    if plan.order_by:
        known = set(plan.dimension_aliases) | set(plan.metric_aliases)
        unknown = [spec.alias for spec in plan.order_by if spec.alias not in known]
        if unknown:
            raise UnknownAggregateFieldError(unknown_aggregate_field_message(unknown[0]))
        return tuple(f"-{spec.alias}" if spec.descending else spec.alias for spec in plan.order_by)
    return plan.dimension_aliases


def execute_aggregate_plan(
    plan: AggregateQueryPlan, queryset: models.QuerySet
) -> list[dict[str, Any]]:
    """Run the plan and return one row dict per group.

    One database query. Each dict is keyed by the plan's aliases: the dimension
    aliases hold the group key, the metric aliases hold the aggregates.
    """
    return list(build_aggregate_queryset(plan, queryset))
