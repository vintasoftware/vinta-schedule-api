"""The six aggregate root fields, built from the registry by one factory.

One resolver body serves all six entities. That is the point of the factory:
six hand-written resolvers is how six entities end up with six slightly
different ideas of what a limit, a timezone or a permission check means.

**What the resolver does, in order.** Validate the timezone, resolve the
group-by inputs into plan dimensions, read the document's own selection set to
work out which metrics are worth computing, build the frozen
``AggregateQueryPlan``, narrow the entity's organization-scoped queryset through
the caller's filter, run one grouped query under a statement timeout, and map
each row dict onto the entity's ``*AggregateRow``. It never sums, sorts or
buckets anything itself.

**Only what was asked for is computed.** The selection set decides the metrics,
so ``title { concat }`` adds a ``string_agg`` and nothing else adds one. A field
the document did not name is not in the ``SELECT`` at all, and comes back null.

**Cost is bounded at four independent points**, and this module is where three
of them meet: the mandatory bounded date range lives on the filter input
(Phase 1), ``limit`` is capped at ``MAX_PAGE_SIZE`` by the executor with the
same bound and wording ``queries._slice_qs`` uses, the group key is always the
default ``ORDER BY`` so paging is stable, and the statement timeout below
bounds a query the other three still let through.

**Permissions are the entity's own resource**, the same one that already lets
the caller page the same rows one at a time. An aggregate therefore discloses
nothing a list field would not.
"""

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

from django.conf import settings
from django.db import DatabaseError, connection, models

import strawberry
import strawberry_django
from graphql import GraphQLError
from strawberry.types.nodes import SelectedField, Selection
from strawberry.utils.str_converters import to_camel_case

from organizations.models import Organization
from public_api.aggregations.dimensions import (
    GROUP_BY_INPUT_TYPES,
    AnyGroupByInput,
    ResolvedGroupBy,
    build_group_key,
    resolve_group_by_inputs,
)
from public_api.aggregations.errors import (
    QUERY_TIMEOUT_MESSAGE,
    AggregateTimeoutError,
)
from public_api.aggregations.executor import execute_aggregate_plan
from public_api.aggregations.filters import (
    AppointmentTypeAggregateFilterInput,
    AvailableTimeAggregateFilterInput,
    BlockedTimeAggregateFilterInput,
    CalendarAggregateFilterInput,
    CalendarEventAggregateFilterInput,
    CalendarPoolAggregateFilterInput,
)
from public_api.aggregations.having import (
    HAVING_INPUT_TYPES,
    AnyHavingInput,
    having_is_empty,
    resolve_having,
)
from public_api.aggregations.ordering import (
    ORDER_INPUT_TYPES,
    AnyOrderInput,
    resolve_order_by,
)
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    FilterBounds,
    MetricSpec,
)
from public_api.aggregations.registry import (
    COUNT_ALIAS,
    COUNT_METRIC_NAME,
    FieldKind,
    concat_alias,
    get_registration,
    metric_alias,
    relation_count_alias,
)
from public_api.aggregations.rows import AGGREGATE_ROW_TYPES, relation_count_field_name
from public_api.aggregations.timezone import resolve_timezone
from public_api.aggregations.types import (
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
)
from public_api.constants import AGGREGATE_STATEMENT_TIMEOUT_MS, MAX_PAGE_SIZE
from public_api.models import SystemUser
from public_api.permissions import IsAuthenticated, OrganizationResourceAccess


logger = logging.getLogger(__name__)


#: The filter input each entity's aggregate field accepts. Owned by Phase 1;
#: named here so the factory has one table to read all four per-entity types from.
FILTER_INPUT_TYPES: Mapping[AggregatableEntity, type] = {
    AggregatableEntity.CALENDAR_EVENT: CalendarEventAggregateFilterInput,
    AggregatableEntity.AVAILABLE_TIME: AvailableTimeAggregateFilterInput,
    AggregatableEntity.BLOCKED_TIME: BlockedTimeAggregateFilterInput,
    AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeAggregateFilterInput,
    AggregatableEntity.CALENDAR: CalendarAggregateFilterInput,
    AggregatableEntity.CALENDAR_POOL: CalendarPoolAggregateFilterInput,
}

#: Sub-field of an aggregate type to the operation that computes it. The names
#: are the GraphQL ones, because that is what a selection set carries.
OP_FOR_SELECTION: Mapping[str, AggregateOp] = {
    "sum": AggregateOp.SUM,
    "avg": AggregateOp.AVG,
    "min": AggregateOp.MIN,
    "max": AggregateOp.MAX,
    "concat": AggregateOp.CONCAT,
    "trueCount": AggregateOp.TRUE_COUNT,
    "falseCount": AggregateOp.FALSE_COUNT,
}

#: Row fields that are not metrics: the group key, and the group's own count,
#: which is always computed whether or not the document asked for it.
#: ``COUNT_ALIAS`` and its siblings live in the registry, so a HAVING clause and
#: an order-by name the same columns the selection set does.
GROUP_KEY_SELECTION = "key"
COUNT_SELECTION = "count"

#: Scalar filter attributes safe to record on the plan's ``FilterBounds``. Record
#: ids only — a filter's free-text ``name`` never goes on a plan, because
#: everything on a plan is eligible to reach the audit trail.
AUDITABLE_FILTER_PREDICATES = ("calendar_id", "user_id")


@dataclass(frozen=True, slots=True)
class SelectedMetric:
    """One aggregatable field the document selected, and how to read it back.

    ``op_aliases`` maps each requested operation to the row-dict key holding its
    value. ``concat_aliases`` is separate because ``concat`` carries arguments:
    two selections with different separators are two different aggregates, and
    both land on the same ``StringAggregate``.
    """

    field_name: str
    kind: FieldKind
    op_aliases: Mapping[AggregateOp, str] = field(default_factory=dict)
    concat_aliases: Mapping[tuple[str, bool], str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RowShape:
    """Everything needed to turn a row dict into an ``*AggregateRow``."""

    metrics: tuple[SelectedMetric, ...] = ()
    relation_counts: Mapping[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Reading the document's selection set
# ---------------------------------------------------------------------------


def _iter_fields(selections: Sequence[Selection]) -> Iterator[SelectedField]:
    """Yield every field in a selection set, walking through fragments.

    A caller is free to put ``durationMinutes { sum }`` behind a fragment, and
    an aggregate it asked for through one must still be computed.
    """
    for selection in selections:
        if isinstance(selection, SelectedField):
            yield selection
        else:
            # Inline fragment or fragment spread — both carry their own
            # selections and neither is a field in its own right.
            yield from _iter_fields(selection.selections)


def _root_selections(info: strawberry.Info) -> list[Selection]:
    """Every selection made on the aggregate field currently resolving.

    GraphQL merges field nodes that share a response key, so a document may name
    this field more than once and expect one merged result object. All of those
    nodes arrive here, and all of their selections count — taking only the first
    would drop metrics the caller explicitly asked for.
    """
    merged: list[Selection] = []
    for selection in info.selected_fields:
        if isinstance(selection, SelectedField):
            merged.extend(selection.selections)
    return merged


def _concat_options(selection: SelectedField) -> tuple[str, bool]:
    """The ``(separator, distinct)`` pair one ``concat`` selection asked for.

    The defaults match ``StringAggregate.concat``'s own, so an argument the
    document omitted is keyed the same way the resolver will look it up.
    """
    arguments = selection.arguments or {}
    separator = arguments.get("separator")
    distinct = arguments.get("distinct")
    return (
        "," if separator is None else str(separator),
        bool(distinct),
    )


def collect_metrics(
    entity: AggregatableEntity, selections: Sequence[Selection]
) -> tuple[tuple[MetricSpec, ...], RowShape]:
    """Turn a row selection set into the metrics to compute and how to read them.

    The group's ``count`` is always computed. It is one ``COUNT(id)`` over rows
    the query is already scanning, ``count`` is non-null on every row type, and
    a plan whose document selected only the group key would otherwise have no
    metrics at all.

    A field name that appears more than once is **merged**, not skipped. GraphQL
    merges selection sets sharing a response key, so ``a: title { concat }`` next
    to ``b: title { min }``, or one ``title { concat }`` beside a fragment adding
    ``title { min }``, is a single ``title`` in the response carrying both. Taking
    only the first occurrence would hand the caller a null for a metric it named
    — indistinguishable from "no rows", and wrong.
    """
    registration = get_registration(entity)
    metrics: list[MetricSpec] = [
        MetricSpec(alias=COUNT_ALIAS, field_path=COUNT_METRIC_NAME, op=AggregateOp.COUNT)
    ]

    # GraphQL names arrive camelCased; the registry is keyed by the snake_case
    # name, so match on the camelCase form rather than converting back.
    metric_fields = {to_camel_case(name): name for name in registration.aggregatable}
    relation_fields = {
        to_camel_case(relation_count_field_name(name)): name
        for name in registration.relation_counts
    }

    # Keyed by registry name and ordered by first appearance, so a repeat of a
    # field accumulates into the entry the first one opened.
    kinds: dict[str, FieldKind] = {}
    op_aliases: dict[str, dict[AggregateOp, str]] = {}
    concat_aliases: dict[str, dict[tuple[str, bool], str]] = {}
    relation_counts: dict[str, str] = {}

    for selection in _iter_fields(selections):
        if selection.name in relation_fields:
            relation_name = relation_fields[selection.name]
            if relation_name in relation_counts:
                # Already counted; a second mention reads the same column.
                continue
            alias = relation_count_alias(relation_name)
            relation_counts[relation_name] = alias
            metrics.append(MetricSpec(alias=alias, field_path=relation_name, op=AggregateOp.COUNT))
            continue

        field_name = metric_fields.get(selection.name)
        if field_name is None:
            # ``key``, ``count``, ``__typename`` or a field this entity does not
            # aggregate. The schema already refused anything genuinely unknown.
            continue

        registered = registration.metric_field(field_name)
        kinds.setdefault(field_name, registered.kind)
        field_ops = op_aliases.setdefault(field_name, {})
        field_concats = concat_aliases.setdefault(field_name, {})

        for sub in _iter_fields(selection.selections):
            op = OP_FOR_SELECTION.get(sub.name)
            if op is None or not registered.supports(op):
                continue
            if op is AggregateOp.CONCAT:
                options = _concat_options(sub)
                if options in field_concats:
                    # The same separator and distinctness is the same column.
                    continue
                alias = concat_alias(field_name, len(field_concats))
                field_concats[options] = alias
                metrics.append(
                    MetricSpec(
                        alias=alias,
                        field_path=field_name,
                        op=op,
                        options={"separator": options[0], "distinct": options[1]},
                    )
                )
                continue
            if op in field_ops:
                continue
            alias = metric_alias(field_name, op)
            field_ops[op] = alias
            metrics.append(MetricSpec(alias=alias, field_path=field_name, op=op))

    selected = tuple(
        SelectedMetric(
            field_name=field_name,
            kind=kinds[field_name],
            op_aliases=op_aliases[field_name],
            concat_aliases=concat_aliases[field_name],
        )
        for field_name in kinds
        if op_aliases[field_name] or concat_aliases[field_name]
    )

    return tuple(metrics), RowShape(metrics=selected, relation_counts=relation_counts)


# ---------------------------------------------------------------------------
# Turning a row dict into an ``*AggregateRow``
# ---------------------------------------------------------------------------


def _operation_value(metric: SelectedMetric, op: AggregateOp, row: Mapping[str, Any]) -> Any:
    """The row value for one operation, or ``None`` when it was not selected.

    An operation the document did not name has no alias and therefore no column,
    so there is nothing to read rather than a value that happens to be null.
    """
    alias = metric.op_aliases.get(op)
    return row.get(alias) if alias is not None else None


def _aggregate_value(metric: SelectedMetric, row: Mapping[str, Any]) -> object:
    """The aggregate object one selected field comes back as."""
    match metric.kind:
        case FieldKind.NUMERIC:
            return NumericAggregate(
                sum=_operation_value(metric, AggregateOp.SUM, row),
                avg=_operation_value(metric, AggregateOp.AVG, row),
                min=_operation_value(metric, AggregateOp.MIN, row),
                max=_operation_value(metric, AggregateOp.MAX, row),
            )
        case FieldKind.STRING:
            return StringAggregate(
                min=_operation_value(metric, AggregateOp.MIN, row),
                max=_operation_value(metric, AggregateOp.MAX, row),
                concat_values={
                    options: row.get(alias) for options, alias in metric.concat_aliases.items()
                },
            )
        case FieldKind.TEMPORAL:
            return DateTimeAggregate(
                min=_operation_value(metric, AggregateOp.MIN, row),
                max=_operation_value(metric, AggregateOp.MAX, row),
            )
        case FieldKind.BOOLEAN:
            return BooleanAggregate(
                true_count=_operation_value(metric, AggregateOp.TRUE_COUNT, row) or 0,
                false_count=_operation_value(metric, AggregateOp.FALSE_COUNT, row) or 0,
            )


def build_row(
    entity: AggregatableEntity,
    resolved: Sequence[ResolvedGroupBy],
    shape: RowShape,
    row: Mapping[str, Any],
) -> Any:
    """Map one grouped row dict onto the entity's ``*AggregateRow``.

    Mapping only. Every number in the row was computed by Postgres.
    """
    row_type = AGGREGATE_ROW_TYPES[entity]
    values: dict[str, Any] = {
        "key": build_group_key(entity, resolved, row),
        "count": row[COUNT_ALIAS],
    }
    for metric in shape.metrics:
        values[metric.field_name] = _aggregate_value(metric, row)
    for relation_name, alias in shape.relation_counts.items():
        values[relation_count_field_name(relation_name)] = row.get(alias)
    return row_type(**values)


# ---------------------------------------------------------------------------
# Cost guard: the statement timeout
# ---------------------------------------------------------------------------


def _timeout_milliseconds() -> int:
    return getattr(
        settings,
        "PUBLIC_API_AGGREGATE_STATEMENT_TIMEOUT_MS",
        AGGREGATE_STATEMENT_TIMEOUT_MS,
    )


@contextmanager
def statement_timeout(milliseconds: int) -> Iterator[None]:
    """Cap how long one aggregate statement may run, then put the setting back.

    ``is_local`` is true, so an aborted transaction rolls the setting back on its
    own; the explicit restore is for the ordinary path, where the request's
    transaction continues into other resolvers that should not inherit an
    aggregate's budget.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('statement_timeout')")
        fetched = cursor.fetchone()
        previous = (fetched[0] if fetched else None) or "0"
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", [f"{milliseconds}ms"])
    try:
        yield
    finally:
        _restore_statement_timeout(previous)


def _restore_statement_timeout(previous: str) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", [previous])
    except DatabaseError:
        # The transaction is already broken — the cancellation that got us here
        # aborted it — and Postgres discards a LOCAL setting with it, so there
        # is nothing left to restore.
        logger.debug("Could not restore statement_timeout; the transaction is already aborted.")


def _is_query_cancelled(error: DatabaseError) -> bool:
    """True when Postgres cancelled the statement on its timeout (SQLSTATE 57014)."""
    return getattr(error.__cause__, "sqlstate", None) == "57014"


# ---------------------------------------------------------------------------
# The shared resolver and the factory
# ---------------------------------------------------------------------------


def _filter_bounds(filter_input: Any) -> FilterBounds:
    """What the filter narrowed to, in the shape the audit trail will read.

    Record ids and the date range only. A filter's ``name`` is free text a
    partner typed and never reaches a plan.
    """
    predicates = {
        name: value
        for name in AUDITABLE_FILTER_PREDICATES
        if (value := getattr(filter_input, name, None)) is not None
    }
    return FilterBounds(
        start=getattr(filter_input, "start_datetime", None),
        end=getattr(filter_input, "end_datetime", None),
        predicates=predicates,
    )


def _base_queryset(entity: AggregatableEntity, organization: Organization) -> models.QuerySet:
    """The entity's organization-scoped rows, before the caller's filter.

    ``filter_by_organization`` names the tenant explicitly rather than leaning on
    the context binding alone. Never ``original_manager``, never ``unscoped()``:
    a ``GROUP BY`` over unscoped rows returns plausible numbers that quietly
    include another organization's.
    """
    registration = get_registration(entity)
    return registration.model.objects.filter_by_organization(  # type: ignore[attr-defined]
        organization.id
    )


def _merge_metrics(*groups: Sequence[MetricSpec]) -> tuple[MetricSpec, ...]:
    """One metric per alias, in first-seen order.

    A clause may name a metric the document did not select, and a document may
    select one no clause names. Both end up in the ``SELECT``; the aliases are
    canonical, so naming the same aggregate twice is one column rather than a
    duplicate the plan's uniqueness check would reject.
    """
    merged: dict[str, MetricSpec] = {}
    for group in groups:
        for metric in group:
            merged.setdefault(metric.alias, metric)
    return tuple(merged.values())


def _resolve_aggregate(
    entity: AggregatableEntity,
    info: strawberry.Info,
    filter_input: Any,
    group_by: Sequence[AnyGroupByInput],
    timezone: str,
    having: AnyHavingInput | None,
    order_by: Sequence[AnyOrderInput] | None,
    limit: int,
    offset: int,
) -> list[Any]:
    """The one resolver body all six aggregate fields share."""
    request = info.context.request
    organization = getattr(request, "public_api_organization", None)
    if organization is None:
        # Same wording ``queries._get_org`` uses; reached only if the middleware
        # bound nothing, which the permission classes already refuse.
        raise GraphQLError("Organization not found in request context")

    system_user: SystemUser | None = getattr(request, "public_api_system_user", None)
    tzinfo = resolve_timezone(timezone)
    resolved = resolve_group_by_inputs(entity, group_by, tzinfo)

    selected_metrics, shape = collect_metrics(entity, _root_selections(info))
    having_spec, having_metrics = resolve_having(entity, having)
    if having_spec is not None and having_is_empty(having_spec):
        # ``having: {}`` is a legal document meaning "no constraint". Dropping it
        # here keeps such a query identical to one that omitted the argument.
        having_spec = None
    order_specs, order_metrics = resolve_order_by(entity, order_by, resolved)

    plan = AggregateQueryPlan(
        entity=entity,
        dimensions=tuple(one.dimension for one in resolved),
        metrics=_merge_metrics(selected_metrics, having_metrics, order_metrics),
        filter_bounds=_filter_bounds(filter_input),
        having=having_spec,
        order_by=order_specs,
        limit=limit,
        offset=offset,
    )

    queryset = filter_input.apply(_base_queryset(entity, organization), organization, system_user)

    try:
        with statement_timeout(_timeout_milliseconds()):
            rows = execute_aggregate_plan(plan, queryset)
    except DatabaseError as error:
        if _is_query_cancelled(error):
            raise AggregateTimeoutError(QUERY_TIMEOUT_MESSAGE) from None
        raise

    return [build_row(entity, resolved, shape, row) for row in rows]


def _make_resolver(entity: AggregatableEntity) -> Callable[..., list[Any]]:
    """Build the entity's resolver, typed for Strawberry from the registry.

    The annotations are assigned rather than written out because they are what
    differs between the six fields — the body below does not.
    """
    filter_type = FILTER_INPUT_TYPES[entity]
    group_by_type = GROUP_BY_INPUT_TYPES[entity]
    having_type = HAVING_INPUT_TYPES[entity]
    order_type = ORDER_INPUT_TYPES[entity]
    row_type = AGGREGATE_ROW_TYPES[entity]

    def resolver(
        info: strawberry.Info,
        filter_: Any,
        group_by: Any,
        timezone: str,
        having: Any = None,
        order_by: Any = None,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> list[Any]:
        return _resolve_aggregate(
            entity, info, filter_, group_by, timezone, having, order_by, limit, offset
        )

    resolver.__name__ = f"{entity.value.lower()}_aggregate"
    resolver.__annotations__ = {
        "info": strawberry.Info,
        # ``filter`` is a builtin, so the parameter carries a trailing
        # underscore and the GraphQL name is set explicitly.
        "filter_": Annotated[filter_type, strawberry.argument(name="filter")],
        "group_by": list[group_by_type],  # type: ignore[valid-type]
        "timezone": str,
        # Both optional: omitting them leaves the field behaving exactly as it
        # did before this phase.
        "having": having_type | None,
        "order_by": list[order_type] | None,  # type: ignore[valid-type]
        "limit": int,
        "offset": int,
        "return": list[row_type],  # type: ignore[valid-type]
    }
    return resolver


def build_aggregate_field(entity: AggregatableEntity) -> Any:
    """Build one entity's aggregate root field, ready to hang on ``Query``."""
    registration = get_registration(entity)
    return strawberry_django.field(
        resolver=_make_resolver(entity),
        permission_classes=[IsAuthenticated, OrganizationResourceAccess],
        description=(
            f"Group {registration.model._meta.verbose_name_plural} by one or more "
            "dimensions and read type-appropriate aggregates over each group. "
            "Requires the same resource scope as the entity's list field. "
            "Buckets are sparse: a period with no matching rows produces no row."
        ),
    )


def aggregate_field_name(entity: AggregatableEntity) -> str:
    """The GraphQL name the entity's aggregate field is registered under."""
    return to_camel_case(get_registration(entity).graphql_field_name)
