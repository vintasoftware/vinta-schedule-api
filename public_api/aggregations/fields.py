"""The six aggregate root fields, built once from the registry.

This is where the engine becomes reachable. Everything before it -- the plan,
the registry, the filters, the dimensions, the executor -- is entity-agnostic
machinery with no GraphQL surface; this module gives each of the six registered
entities a root field, and does it from one factory so the six share one
resolver body. Six hand-written resolvers would be six places for a guard to be
forgotten, and the guards are the point: an aggregate is the most expensive
thing this API can be asked to do.

**What a row type looks like.** Each entity gets one `*AggregateRow`, built from
its registration: `key` (the group key from
:mod:`public_api.aggregations.dimensions`), a non-null `count`, one field per
aggregatable field typed as the aggregate type its kind maps to, and one `Int`
per registered relation count. Because the type is generated, a field added to
the registry appears on the schema without a second edit -- and cannot appear
with an operation set its type does not offer, because the *type* carries the
operations.

**What the resolver does.** It reads the metrics off the GraphQL selection: a
document that asks for `durationMinutes { sum avg }` produces exactly two
aggregate annotations, and one that asks for none produces only the row count.
Asking the database for aggregates nobody selected is the other half of
bounding cost.

**The four cost guards**, in the order they apply:

1. `limit` / `offset` through :func:`public_api.pagination.validate_pagination`,
   the same 1-100 bound -- and the same message -- every list field on this API
   applies. Checked before the queryset is built, so an out-of-range window
   costs no database work at all.
2. The mandatory bounded date range, which the Phase 1 filter inputs carry as
   non-null fields and enforce against `MAX_AGGREGATE_RANGE`.
3. A deterministic `ORDER BY` on the full group key, so paging returns each
   group once. This phase sends no `order_by` on the plan, and
   :func:`public_api.aggregations.executor.build_aggregate_queryset` orders on
   the group key when the plan names no ordering -- see `_order_by` there.
4. A per-query statement timeout, applied around aggregate execution only.

Tenancy is the filter input's, and stays there: `apply()` starts from the
model's organization-scoped manager and narrows to the token's own calendars.
Nothing here widens it.
"""

import contextlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from django.db import OperationalError, connection, transaction

import strawberry
import strawberry_django
from graphql import GraphQLError
from strawberry.types.nodes import SelectedField, Selection
from strawberry.utils.str_converters import to_camel_case

from public_api.aggregations.dimensions import (
    GROUP_BY_INPUT_TYPE_BY_ENTITY,
    GROUP_KEY_TYPE_BY_ENTITY,
    build_group_key,
    dimensions_from_group_by,
)
from public_api.aggregations.errors import AggregateQueryTimeoutError
from public_api.aggregations.executor import execute_plan
from public_api.aggregations.filters import (
    AggregateFilterValidationError,
    AppointmentTypeAggregateFilterInput,
    AvailableTimeAggregateFilterInput,
    BlockedTimeAggregateFilterInput,
    CalendarAggregateFilterInput,
    CalendarEventAggregateFilterInput,
    CalendarPoolAggregateFilterInput,
)
from public_api.aggregations.having import HAVING_INPUT_TYPE_BY_ENTITY, having_from_input
from public_api.aggregations.ordering import (
    ORDER_INPUT_TYPE_BY_ENTITY,
    order_from_input,
)
from public_api.aggregations.plan import (
    MAX_LIMIT,
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    FilterBounds,
    MetricSpec,
    ParentKeySpec,
)
from public_api.aggregations.registry import (
    AggregateKind,
    EntityRegistration,
    get_registration,
    metric_alias,
)
from public_api.aggregations.types import (
    DEFAULT_CONCAT_SEPARATOR,
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
)
from public_api.aggregations.windows import (
    WINDOW_INPUT_TYPE_BY_ENTITY,
    WINDOW_METRIC_DEFINITIONS_BY_ENTITY,
    WINDOW_METRICS_TYPE_BY_ENTITY,
    build_window_metrics,
    window_from_input,
)
from public_api.constants import AGGREGATE_STATEMENT_TIMEOUT_MS, PublicAPIResources
from public_api.pagination import validate_pagination
from public_api.permissions import IsAuthenticated, OrganizationResourceAccess


# Postgres' SQLSTATE for a statement the server itself cancelled, which is what
# a lapsed `statement_timeout` produces. Matched rather than the message, which
# is localized.
_QUERY_CANCELED_SQLSTATE = "57014"

# The alias the group's row count lands under. Always requested: `count` is
# non-null on every row type, and a plan needs at least one metric, so a
# document that selects nothing but `key` still has something to aggregate.
_ROW_COUNT_ALIAS = "count"

# Which aggregate operation each sub-selection of an aggregate type names.
# Keys are the GraphQL field names, because that is what a selection carries.
_OP_BY_SELECTION_NAME: Mapping[str, AggregateOp] = MappingProxyType(
    {
        "sum": AggregateOp.SUM,
        "avg": AggregateOp.AVG,
        "min": AggregateOp.MIN,
        "max": AggregateOp.MAX,
        "concat": AggregateOp.CONCAT,
        "trueCount": AggregateOp.TRUE_COUNT,
        "falseCount": AggregateOp.FALSE_COUNT,
    }
)

# Each entity's filter input. The group-by inputs and group key types come from
# `dimensions.py`, which already publishes them per entity.
FILTER_INPUT_TYPE_BY_ENTITY: Mapping[AggregatableEntity, type] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: CalendarEventAggregateFilterInput,
        AggregatableEntity.AVAILABLE_TIME: AvailableTimeAggregateFilterInput,
        AggregatableEntity.BLOCKED_TIME: BlockedTimeAggregateFilterInput,
        AggregatableEntity.APPOINTMENT_TYPE: AppointmentTypeAggregateFilterInput,
        AggregatableEntity.CALENDAR: CalendarAggregateFilterInput,
        AggregatableEntity.CALENDAR_POOL: CalendarPoolAggregateFilterInput,
    }
)

# The resource a token must hold to aggregate an entity: the *same* one its
# existing list field requires. An aggregate discloses less about a row than a
# list of the rows themselves does, so a separate grant would be a second thing
# to get wrong without being a second thing to protect.
AGGREGATE_RESOURCE_BY_ENTITY: Mapping[AggregatableEntity, str] = MappingProxyType(
    {
        AggregatableEntity.CALENDAR_EVENT: PublicAPIResources.CALENDAR_EVENT,
        AggregatableEntity.AVAILABLE_TIME: PublicAPIResources.AVAILABLE_TIME,
        AggregatableEntity.BLOCKED_TIME: PublicAPIResources.BLOCKED_TIME,
        AggregatableEntity.APPOINTMENT_TYPE: PublicAPIResources.APPOINTMENT_TYPE,
        AggregatableEntity.CALENDAR: PublicAPIResources.CALENDAR,
        AggregatableEntity.CALENDAR_POOL: PublicAPIResources.CALENDAR_POOL,
    }
)


def entity_class_prefix(entity: AggregatableEntity) -> str:
    """`calendar_event` -> `CalendarEvent`, the prefix every type name shares."""
    return "".join(part.title() for part in entity.value.split("_"))


# The Python attribute each field is registered under on `Query`, and therefore
# -- after Strawberry camel-cases it -- the GraphQL field name. Published so the
# permission mapping can be checked against the registry rather than against a
# hand-written list.
AGGREGATE_ATTRIBUTE_NAME_BY_ENTITY: Mapping[AggregatableEntity, str] = MappingProxyType(
    {entity: f"{entity.value}_aggregate" for entity in AggregatableEntity}
)

AGGREGATE_FIELD_NAME_BY_ENTITY: Mapping[AggregatableEntity, str] = MappingProxyType(
    {
        entity: to_camel_case(attribute)
        for entity, attribute in AGGREGATE_ATTRIBUTE_NAME_BY_ENTITY.items()
    }
)


# ---------------------------------------------------------------------------
# Row output types
# ---------------------------------------------------------------------------


def _build_row_type(registration: EntityRegistration) -> type:
    """Build one entity's `*AggregateRow` from its registration.

    Generated rather than written out six times because the registry already
    holds every fact the type needs: which fields are aggregatable, and which
    of the four aggregate types each one's kind maps to. Writing them by hand
    would let the schema disagree with the engine, which is the failure the
    registry's import-time checks exist to prevent everywhere else.
    """
    prefix = entity_class_prefix(registration.entity)
    annotations: dict[str, Any] = {"key": GROUP_KEY_TYPE_BY_ENTITY[registration.entity]}
    namespace: dict[str, Any] = {
        "key": strawberry.field(description="The dimensions this row was grouped by."),
    }

    annotations["count"] = int
    namespace["count"] = strawberry.field(
        default=0, description="How many rows fell into this group."
    )

    for spec in registration.aggregatable:
        annotations[spec.name] = spec.graphql_type | None
        namespace[spec.name] = strawberry.field(default=None, description=spec.description or None)

    for relation in registration.relation_counts:
        annotations[relation.name] = int | None
        namespace[relation.name] = strawberry.field(
            default=None, description=relation.description or None
        )

    annotations["window"] = WINDOW_METRICS_TYPE_BY_ENTITY[registration.entity] | None
    namespace["window"] = strawberry.field(
        default=None,
        description=(
            "Values computed over this row's place in the ordered sequence of groups. "
            "Null unless the field was given a `window` argument."
        ),
    )

    namespace["__annotations__"] = annotations
    namespace["__module__"] = __name__
    namespace["__doc__"] = f"One grouped row of a {prefix} aggregate."

    row_class = type(f"{prefix}AggregateRow", (), namespace)
    return strawberry.type(
        row_class,
        description=(
            f"One group of a {prefix} aggregate: the dimensions it was grouped by, "
            f"and the aggregates computed over it."
        ),
    )


AGGREGATE_ROW_TYPE_BY_ENTITY: Mapping[AggregatableEntity, type] = MappingProxyType(
    {entity: _build_row_type(get_registration(entity)) for entity in AggregatableEntity}
)


# ---------------------------------------------------------------------------
# Selection -> metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SelectedMetric:
    """One row-type field, and the row keys its operations landed under.

    Carried from plan construction through to row construction so the two agree
    by sharing an object rather than by both calling
    :func:`~public_api.aggregations.registry.metric_alias` and hoping.
    """

    row_field: str
    kind: AggregateKind | None
    aliases: Mapping[AggregateOp, str]
    concat_options: Mapping[str, Any]


def _flatten(selections: Sequence[Selection]) -> Iterator[SelectedField]:
    """Yield the fields a selection set names, seeing through fragments.

    A partner may perfectly well ask for its aggregates through an inline
    fragment or a named one; neither is a field, and neither should make a
    metric go missing.
    """
    for selection in selections:
        if isinstance(selection, SelectedField):
            yield selection
        else:
            yield from _flatten(selection.selections)


def _root_selections(info: strawberry.Info) -> Iterator[SelectedField]:
    """The fields selected on the aggregate root field itself."""
    for field in info.selected_fields:
        yield from _flatten(field.selections)


def _concat_options(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """`concat`'s arguments, defaulted the same way the schema defaults them.

    These change the SQL rather than the presentation, which is why they are
    read here, when the plan is built, rather than when the field resolves.
    """
    return {
        "separator": arguments.get("separator", DEFAULT_CONCAT_SEPARATOR),
        "distinct": bool(arguments.get("distinct", False)),
    }


def _window_metrics_from_selection(
    entity: AggregatableEntity, info: strawberry.Info
) -> tuple[str, ...]:
    """Which fields of the `*WindowMetrics` type the document asked for.

    A window function nobody selected is not computed, for the same reason an
    unselected aggregate is not: the `OVER` clause is the expensive half of the
    query, and asking the database for four of them to return one is the sort
    of cost this plan bounds everywhere else.
    """
    known = {
        to_camel_case(definition.name): definition.name
        for definition in WINDOW_METRIC_DEFINITIONS_BY_ENTITY[entity]
    }
    selected: list[str] = []
    for selection in _root_selections(info):
        if selection.name != "window":
            continue
        for window_field in _flatten(selection.selections):
            name = known.get(window_field.name)
            if name is not None and name not in selected:
                selected.append(name)
    return tuple(selected)


def _metrics_from_selection(
    registration: EntityRegistration, info: strawberry.Info
) -> tuple[tuple[MetricSpec, ...], tuple[_SelectedMetric, ...]]:
    """Turn the GraphQL selection into the metrics to aggregate.

    Only what the document selected is computed. The row count is the one
    exception -- it is always requested, because `count` is non-null on every
    row type and because a plan with no metrics is refused.

    At most one `concat` is built per field: the row type carries one
    `StringAggregate` per field, so a second `concat` on the same field with
    different arguments has nowhere to put its string. That case is caught by
    `StringAggregate.concat` itself, which compares the arguments it resolves
    with against the ones the query was built for.
    """
    metrics: list[MetricSpec] = [MetricSpec.row_count(alias=_ROW_COUNT_ALIAS)]
    selected: list[_SelectedMetric] = []

    field_by_graphql_name = {to_camel_case(spec.name): spec for spec in registration.aggregatable}
    relation_by_graphql_name = {
        to_camel_case(relation.name): relation for relation in registration.relation_counts
    }

    seen_row_fields: set[str] = set()
    for selection in _root_selections(info):
        relation = relation_by_graphql_name.get(selection.name)
        if relation is not None:
            if relation.name in seen_row_fields:
                continue
            seen_row_fields.add(relation.name)
            alias = metric_alias(relation.name, AggregateOp.COUNT)
            metrics.append(MetricSpec(alias=alias, field_path=relation.name, op=AggregateOp.COUNT))
            selected.append(
                _SelectedMetric(
                    row_field=relation.name,
                    kind=None,
                    aliases=MappingProxyType({AggregateOp.COUNT: alias}),
                    concat_options=MappingProxyType({}),
                )
            )
            continue

        spec = field_by_graphql_name.get(selection.name)
        if spec is None or spec.name in seen_row_fields:
            # `key`, `count`, `__typename`, or a field selected twice. The
            # second selection of a field asks for nothing the first did not.
            continue
        seen_row_fields.add(spec.name)

        aliases: dict[AggregateOp, str] = {}
        concat_options: dict[str, Any] = {}
        for operation_selection in _flatten(selection.selections):
            op = _OP_BY_SELECTION_NAME.get(operation_selection.name)
            if op is None or op not in spec.ops or op in aliases:
                continue
            options = (
                _concat_options(operation_selection.arguments) if op is AggregateOp.CONCAT else {}
            )
            alias = metric_alias(spec.name, op, options)
            aliases[op] = alias
            if op is AggregateOp.CONCAT:
                concat_options = options
            metrics.append(MetricSpec(alias=alias, field_path=spec.name, op=op, options=options))

        # A field selected with no operation under it (only `__typename`, say)
        # still gets an entry: nothing is aggregated, but the row carries the
        # type so the response shape matches the document.
        selected.append(
            _SelectedMetric(
                row_field=spec.name,
                kind=spec.kind,
                aliases=MappingProxyType(aliases),
                concat_options=MappingProxyType(concat_options),
            )
        )

    return tuple(metrics), tuple(selected)


# ---------------------------------------------------------------------------
# Row -> GraphQL type
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    """Numeric aggregates publish floats; Postgres returns ints and decimals."""
    return None if value is None else float(value)


def _aggregate_value(selected: _SelectedMetric, row: Mapping[str, Any]) -> Any:
    """One aggregate sub-object, filled from the row the database returned.

    An operation the document did not select has no alias and stays `None`,
    which is also what SQL returns for an aggregate over a group of nulls --
    the two are indistinguishable on purpose, because neither is an error.
    """

    def value(op: AggregateOp) -> Any:
        alias = selected.aliases.get(op)
        return None if alias is None else row.get(alias)

    if selected.kind is AggregateKind.NUMERIC:
        return NumericAggregate(
            sum=_as_float(value(AggregateOp.SUM)),
            avg=_as_float(value(AggregateOp.AVG)),
            min=_as_float(value(AggregateOp.MIN)),
            max=_as_float(value(AggregateOp.MAX)),
        )
    if selected.kind is AggregateKind.DATETIME:
        return DateTimeAggregate(min=value(AggregateOp.MIN), max=value(AggregateOp.MAX))
    if selected.kind is AggregateKind.STRING:
        return StringAggregate(
            min=value(AggregateOp.MIN),
            max=value(AggregateOp.MAX),
            concat_value=value(AggregateOp.CONCAT),
            concat_separator=selected.concat_options.get("separator", DEFAULT_CONCAT_SEPARATOR),
            concat_distinct=bool(selected.concat_options.get("distinct", False)),
        )
    return BooleanAggregate(
        true_count=int(value(AggregateOp.TRUE_COUNT) or 0),
        false_count=int(value(AggregateOp.FALSE_COUNT) or 0),
    )


def _build_row(
    row_type: type,
    plan: AggregateQueryPlan,
    row: Mapping[str, Any],
    selected: tuple[_SelectedMetric, ...],
    window_metric_names: tuple[str, ...] = (),
) -> Any:
    """One aggregated row dict as the entity's `*AggregateRow`."""
    values: dict[str, Any] = {
        "key": build_group_key(plan.entity, plan.dimensions, dict(row)),
        "count": int(row.get(_ROW_COUNT_ALIAS) or 0),
    }
    for metric in selected:
        if metric.kind is None:
            alias = metric.aliases[AggregateOp.COUNT]
            values[metric.row_field] = row.get(alias)
            continue
        values[metric.row_field] = _aggregate_value(metric, row)

    if plan.window is not None and window_metric_names:
        values["window"] = build_window_metrics(plan.entity, window_metric_names, row)

    return row_type(**values)


# ---------------------------------------------------------------------------
# Cost guard: the statement timeout
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def aggregate_statement_timeout(
    milliseconds: int = AGGREGATE_STATEMENT_TIMEOUT_MS,
) -> Iterator[None]:
    """Bound how long the statements inside the block may run.

    `set_config(..., is_local=True)` inside a transaction can never leak onto
    the pooled connection, which is why it is preferred; outside one it would
    be a silent no-op, so the session-level form is used instead and the
    previous value is put back on the way out. Either way the timeout applies
    around aggregate execution only -- the rest of the request keeps whatever
    the connection was configured with.
    """
    is_local = connection.in_atomic_block
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('statement_timeout')")
        fetched = cursor.fetchone()
        previous = fetched[0] if fetched else "0"
        cursor.execute(
            "SELECT set_config('statement_timeout', %s, %s)", [str(milliseconds), is_local]
        )
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('statement_timeout', %s, %s)", [previous, is_local])


def _is_query_canceled(exc: OperationalError) -> bool:
    """Whether this database error is the statement timeout firing."""
    return getattr(exc.__cause__, "sqlstate", None) == _QUERY_CANCELED_SQLSTATE


def _execute_within_budget(plan: AggregateQueryPlan, queryset: Any) -> list[dict[str, Any]]:
    """Run the plan under the statement timeout, mapping a cancel to the
    documented error.

    The execution gets its own savepoint because a cancelled statement aborts
    its transaction: rolling back to the savepoint is what leaves the
    connection usable for the timeout's own restore, and for the rest of a
    request that Django is already running inside a transaction.
    """
    with aggregate_statement_timeout():
        try:
            with transaction.atomic():
                return execute_plan(plan, queryset)
        except OperationalError as exc:
            if _is_query_canceled(exc):
                raise AggregateQueryTimeoutError from None
            raise


# ---------------------------------------------------------------------------
# The shared resolver body
# ---------------------------------------------------------------------------


def _filter_bounds(filter_input: Any) -> FilterBounds:
    """What the caller narrowed to, for the audit trail a later phase writes.

    Record ids only. A filter's free-text and enum predicates are deliberately
    left out: this object is built to be serialized into an audit record, and a
    record of who asked what should not become a second copy of what they asked
    about.
    """
    predicates = {
        name: getattr(filter_input, name)
        for name in ("calendar_id", "user_id", "pool_id")
        if getattr(filter_input, name, None) is not None
    }
    return FilterBounds(
        start=getattr(filter_input, "start_datetime", None),
        end=getattr(filter_input, "end_datetime", None),
        predicates=predicates,
    )


def _merge_metrics(
    selected: Sequence[MetricSpec], *additional: Sequence[MetricSpec]
) -> tuple[MetricSpec, ...]:
    """The selection's metrics, plus any a `having` or `orderBy` needs.

    Deduplicated by alias, keeping the first: a metric the document both
    selected and filtered on is one annotation, and two `MetricSpec`s under one
    alias would be an `AliasCollisionError` rather than a merge.
    """
    merged: dict[str, MetricSpec] = {spec.alias: spec for spec in selected}
    for group in additional:
        for spec in group:
            merged.setdefault(spec.alias, spec)
    return tuple(merged.values())


@dataclass(frozen=True)
class AggregateRequest:
    """Everything one aggregate field needs, resolved and not yet executed.

    Built by :func:`build_aggregate_request` and consumed by
    :func:`execute_request` / :func:`build_rows`. The three exist as separate
    steps because :mod:`public_api.aggregations.nested` runs them apart: it
    narrows the queryset to a batch of parents between the first and the second,
    and splits the rows per parent between the second and the third. A root
    field runs all three back to back.
    """

    plan: AggregateQueryPlan
    queryset: Any
    selected: tuple[_SelectedMetric, ...]
    window_metric_names: tuple[str, ...]
    row_type: type

    def narrowed(self, queryset: Any) -> "AggregateRequest":
        """The same request over a further-narrowed queryset."""
        return replace(self, queryset=queryset)


def build_aggregate_request(
    entity: AggregatableEntity,
    info: strawberry.Info,
    filter_input: Any,
    group_by: Sequence[Any],
    timezone: str,
    having: Any,
    order_by: Sequence[Any] | None,
    window: Any,
    limit: int,
    offset: int,
    parent_key: ParentKeySpec | None = None,
) -> AggregateRequest:
    """Turn one aggregate field's arguments and selection into a request.

    ``parent_key`` is set only by the nested collector, which folds the parent's
    column into the ``GROUP BY`` so that one query answers for every parent at
    that level. Everything else is identical whether the field sits at the root
    or under a parent -- including all four cost guards, which is the point of
    there being one of these functions rather than two.
    """
    registration = get_registration(entity)

    # Cost guard 1, before anything touches the database.
    validate_pagination(offset, limit)

    request = info.context.request
    organization = getattr(request, "public_api_organization", None)
    if organization is None:
        # `OrganizationResourceAccess` already refuses a request without one;
        # this is the guard for a field that somehow ran without it.
        raise GraphQLError("Organization not found in request context")
    system_user = getattr(request, "public_api_system_user", None)

    # Cost guard 2, and the whole of tenancy: the filter starts from the
    # model's organization-scoped manager and narrows to the token's calendars.
    try:
        queryset = filter_input.apply(system_user, organization)
    except AggregateFilterValidationError as exc:
        # Raised as a plain exception by the filter inputs; a partner-facing
        # condition deserves a partner-facing error rather than a 500.
        raise GraphQLError(str(exc)) from None

    dimensions = dimensions_from_group_by(entity, group_by, timezone)
    metrics, selected = _metrics_from_selection(registration, info)

    # A `having` or an `orderBy` may name a metric the document did not select.
    # Both converters therefore hand back the metrics their clause reads, and
    # those are merged into the plan -- an alias that is not annotated resolves
    # against the model instead, which turns a `HAVING` into a `WHERE` and an
    # `ORDER BY` on an aggregate into one on a column.
    having_spec, having_metrics = having_from_input(registration, having)
    order_specs, order_metrics = order_from_input(entity, order_by, dimensions)

    # Windows read the same kind of unselected metric a `having` does -- a
    # running total over `durationMinutes` needs that sum annotated whether or
    # not the document displays it -- so the converter hands its metrics back
    # the same way and they are merged in the same place.
    window_metric_names = _window_metrics_from_selection(entity, info)
    window_spec, window_metrics = window_from_input(entity, window, window_metric_names, dimensions)

    # Whatever the caller did not order by, the executor appends from the group
    # key -- cost guard 3. Paging a grouped result whose ordering does not
    # fully determine row order returns overlapping and missing groups between
    # two otherwise identical calls.
    plan = AggregateQueryPlan(
        entity=entity,
        dimensions=dimensions,
        metrics=_merge_metrics(metrics, having_metrics, order_metrics, window_metrics),
        filter_bounds=_filter_bounds(filter_input),
        having=having_spec,
        order_by=order_specs,
        window=window_spec,
        parent_key=parent_key,
        limit=limit,
        offset=offset,
    )

    return AggregateRequest(
        plan=plan,
        queryset=queryset,
        selected=selected,
        window_metric_names=window_metric_names,
        row_type=AGGREGATE_ROW_TYPE_BY_ENTITY[entity],
    )


def execute_request(request: AggregateRequest) -> list[dict[str, Any]]:
    """Run one request under the statement timeout, returning its raw rows."""
    return _execute_within_budget(request.plan, request.queryset)


def build_rows(request: AggregateRequest, rows: Sequence[Mapping[str, Any]]) -> list[Any]:
    """Map raw grouped rows onto the entity's ``*AggregateRow`` type."""
    return [
        _build_row(
            request.row_type, request.plan, row, request.selected, request.window_metric_names
        )
        for row in rows
    ]


def _resolve_aggregate(
    entity: AggregatableEntity,
    info: strawberry.Info,
    filter_input: Any,
    group_by: Sequence[Any],
    timezone: str,
    having: Any,
    order_by: Sequence[Any] | None,
    window: Any,
    limit: int,
    offset: int,
) -> list[Any]:
    """Resolve one aggregate root field. Shared, unmodified, by all six."""
    request = build_aggregate_request(
        entity, info, filter_input, group_by, timezone, having, order_by, window, limit, offset
    )
    return build_rows(request, execute_request(request))


# ---------------------------------------------------------------------------
# The field factory
# ---------------------------------------------------------------------------


def aggregate_field(entity: AggregatableEntity) -> Any:
    """Build the `strawberry_django.field` that exposes `entity`'s aggregate.

    The resolver's annotations are set after the fact because the argument and
    return types differ per entity while the body does not. That is the whole
    reason this is a factory: six fields, six sets of types, one resolver.
    """
    filter_type = FILTER_INPUT_TYPE_BY_ENTITY[entity]
    group_by_type = GROUP_BY_INPUT_TYPE_BY_ENTITY[entity]
    having_type = HAVING_INPUT_TYPE_BY_ENTITY[entity]
    order_type = ORDER_INPUT_TYPE_BY_ENTITY[entity]
    window_type = WINDOW_INPUT_TYPE_BY_ENTITY[entity]
    row_type = AGGREGATE_ROW_TYPE_BY_ENTITY[entity]
    prefix = entity_class_prefix(entity)

    def resolver(
        info: strawberry.Info,
        filter: Any,  # noqa: A002 -- the GraphQL argument this plan specifies is `filter`
        group_by: Sequence[Any],
        timezone: str,
        having: Any = None,
        order_by: Sequence[Any] | None = None,
        window: Any = None,
        limit: int = MAX_LIMIT,
        offset: int = 0,
    ) -> list[Any]:
        return _resolve_aggregate(
            entity, info, filter, group_by, timezone, having, order_by, window, limit, offset
        )

    resolver.__name__ = AGGREGATE_ATTRIBUTE_NAME_BY_ENTITY[entity]
    resolver.__qualname__ = resolver.__name__
    resolver.__annotations__ = {
        "info": strawberry.Info,
        "filter": filter_type,
        "group_by": list[group_by_type],  # type: ignore[valid-type]
        "timezone": str,
        "having": having_type | None,
        "order_by": list[order_type] | None,  # type: ignore[valid-type]
        "window": window_type | None,
        "limit": int,
        "offset": int,
        "return": list[row_type],  # type: ignore[valid-type]
    }

    return strawberry_django.field(
        resolver=resolver,
        permission_classes=[IsAuthenticated, OrganizationResourceAccess],
        description=(
            f"Group {prefix} rows and aggregate over them. `timezone` is the IANA "
            f"name every temporal bucket boundary is computed in. `having` drops "
            f"groups on their aggregated values, `orderBy` sorts them, and `window` "
            f"computes running totals and moving averages across them -- all in "
            f"SQL, and none of them requires the metric it names to be selected. "
            f"The filter's date range is mandatory and bounded, and `limit` is "
            f"capped at {MAX_LIMIT}."
        ),
    )
