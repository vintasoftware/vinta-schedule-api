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
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    FilterBounds,
    MetricSpec,
)
from public_api.aggregations.registry import (
    COUNT_METRIC_NAME,
    FieldKind,
    get_registration,
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
GROUP_KEY_SELECTION = "key"
COUNT_SELECTION = "count"
COUNT_ALIAS = "metric_count"

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


def _root_selection(info: strawberry.Info) -> SelectedField | None:
    """The document's selection of the aggregate field currently resolving."""
    for selection in info.selected_fields:
        if isinstance(selection, SelectedField):
            return selection
    return None


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

    selected: list[SelectedMetric] = []
    relation_counts: dict[str, str] = {}

    for selection in _iter_fields(selections):
        if selection.name in relation_fields:
            relation_name = relation_fields[selection.name]
            if relation_name in relation_counts:
                continue
            alias = f"metric_{relation_name}_count"
            relation_counts[relation_name] = alias
            metrics.append(MetricSpec(alias=alias, field_path=relation_name, op=AggregateOp.COUNT))
            continue

        field_name = metric_fields.get(selection.name)
        if field_name is None:
            # ``key``, ``count``, ``__typename`` or a field this entity does not
            # aggregate. The schema already refused anything genuinely unknown.
            continue
        if any(one.field_name == field_name for one in selected):
            # The same aggregate selected twice (two aliases, or a fragment that
            # repeats it) is one set of columns, not two.
            continue

        registered = registration.metric_field(field_name)
        op_aliases: dict[AggregateOp, str] = {}
        concat_aliases: dict[tuple[str, bool], str] = {}

        for sub in _iter_fields(selection.selections):
            op = OP_FOR_SELECTION.get(sub.name)
            if op is None or not registered.supports(op):
                continue
            if op is AggregateOp.CONCAT:
                options = _concat_options(sub)
                if options in concat_aliases:
                    continue
                alias = f"metric_{field_name}_concat_{len(concat_aliases)}"
                concat_aliases[options] = alias
                metrics.append(
                    MetricSpec(
                        alias=alias,
                        field_path=field_name,
                        op=op,
                        options={"separator": options[0], "distinct": options[1]},
                    )
                )
                continue
            if op in op_aliases:
                continue
            alias = f"metric_{field_name}_{op.value.lower()}"
            op_aliases[op] = alias
            metrics.append(MetricSpec(alias=alias, field_path=field_name, op=op))

        if op_aliases or concat_aliases:
            selected.append(
                SelectedMetric(
                    field_name=field_name,
                    kind=registered.kind,
                    op_aliases=op_aliases,
                    concat_aliases=concat_aliases,
                )
            )

    return tuple(metrics), RowShape(metrics=tuple(selected), relation_counts=relation_counts)


# ---------------------------------------------------------------------------
# Turning a row dict into an ``*AggregateRow``
# ---------------------------------------------------------------------------


def _aggregate_value(metric: SelectedMetric, row: Mapping[str, Any]) -> object:
    """The aggregate object one selected field comes back as."""
    match metric.kind:
        case FieldKind.NUMERIC:
            return NumericAggregate(
                sum=row.get(metric.op_aliases.get(AggregateOp.SUM, "")),
                avg=row.get(metric.op_aliases.get(AggregateOp.AVG, "")),
                min=row.get(metric.op_aliases.get(AggregateOp.MIN, "")),
                max=row.get(metric.op_aliases.get(AggregateOp.MAX, "")),
            )
        case FieldKind.STRING:
            return StringAggregate(
                min=row.get(metric.op_aliases.get(AggregateOp.MIN, "")),
                max=row.get(metric.op_aliases.get(AggregateOp.MAX, "")),
                concat_values={
                    options: row.get(alias) for options, alias in metric.concat_aliases.items()
                },
            )
        case FieldKind.TEMPORAL:
            return DateTimeAggregate(
                min=row.get(metric.op_aliases.get(AggregateOp.MIN, "")),
                max=row.get(metric.op_aliases.get(AggregateOp.MAX, "")),
            )
        case FieldKind.BOOLEAN:
            return BooleanAggregate(
                true_count=row.get(metric.op_aliases.get(AggregateOp.TRUE_COUNT, "")) or 0,
                false_count=row.get(metric.op_aliases.get(AggregateOp.FALSE_COUNT, "")) or 0,
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


def _resolve_aggregate(
    entity: AggregatableEntity,
    info: strawberry.Info,
    filter_input: Any,
    group_by: Sequence[AnyGroupByInput],
    timezone: str,
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

    selection = _root_selection(info)
    metrics, shape = collect_metrics(entity, selection.selections if selection else [])

    plan = AggregateQueryPlan(
        entity=entity,
        dimensions=tuple(one.dimension for one in resolved),
        metrics=metrics,
        filter_bounds=_filter_bounds(filter_input),
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
    row_type = AGGREGATE_ROW_TYPES[entity]

    def resolver(
        info: strawberry.Info,
        filter_: Any,
        group_by: Any,
        timezone: str,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> list[Any]:
        return _resolve_aggregate(entity, info, filter_, group_by, timezone, limit, offset)

    resolver.__name__ = f"{entity.value.lower()}_aggregate"
    resolver.__annotations__ = {
        "info": strawberry.Info,
        # ``filter`` is a builtin, so the parameter carries a trailing
        # underscore and the GraphQL name is set explicitly.
        "filter_": Annotated[filter_type, strawberry.argument(name="filter")],
        "group_by": list[group_by_type],  # type: ignore[valid-type]
        "timezone": str,
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
