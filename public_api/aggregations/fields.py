"""The six aggregate root fields, and the one resolver body they share.

Each entity gets a thin, explicitly-typed wrapper (``calendar_event_aggregate``,
``available_time_aggregate``, ...) so its ``filter`` / ``groupBy`` / return
type are the entity's own -- Strawberry needs a concrete type per field for
schema generation, so there is no way to erase that part into a single
generic function. Everything past argument shape is one function,
``_execute_aggregate``, which every wrapper calls:

1. validate ``limit`` / ``offset`` with the exact wording
   ``public_api.queries._slice_qs`` uses, before a plan is ever built;
2. resolve the organization and system user off the request the same way
   every other field in ``public_api.queries`` does;
3. narrow the entity's scoped queryset through the filter input (Phase 1),
   which is also where the mandatory bounded date range is enforced for the
   three temporal entities;
4. resolve ``groupBy`` into dimensions (Phase 2);
5. read the row selection to decide which metrics to compute -- a caller who
   selects ``durationMinutes { sum avg }`` gets exactly a ``SUM`` and an
   ``AVG`` annotation, not a fixed bundle of every operation a field could
   support. This is also where ``title { concat(separator: "; ") } ``'s
   arguments reach the plan: they are read off the selection, not resolved
   later, because the ``string_agg`` they shape is emitted by the same query
   this function builds;
6. resolve ``having`` and ``orderBy`` (Phase 4). Both can reference a metric
   the row selection never asked for -- ``orderBy: [{metric: COUNT}]`` when
   the caller only wants ``durationMinutes`` in the output, say -- so
   whatever they need is folded into the metric list here, deduplicated the
   same way a doubly-selected row field already is, before the plan is ever
   built. This is what lets such a metric be annotated rather than raising;
7. build the plan and hand it to the executor, which supplies the
   deterministic group-key ``ORDER BY`` cost guard on its own (see its
   ``_ordering_terms``);
8. run the query inside a Postgres statement timeout scoped to this query
   alone -- the last of the plan's four cost guards -- and map each row dict
   onto the entity's ``*AggregateRow`` type.

Tenant scoping is entirely the filter input's job (step 3); nothing here
calls ``unscoped()`` or ``original_manager``.
"""

from collections.abc import Sequence
from typing import Annotated, Any

from django.db import connection, transaction
from django.db.utils import OperationalError

import strawberry
from graphql import GraphQLError
from psycopg.errors import QueryCanceled
from strawberry.types.nodes import FragmentSpread, InlineFragment, SelectedField, Selection

from organizations.models import Organization
from public_api.aggregations.dimensions import (
    AppointmentTypeGroupByInput,
    AvailableTimeGroupByInput,
    BlockedTimeGroupByInput,
    CalendarEventGroupByInput,
    CalendarGroupByInput,
    CalendarPoolGroupByInput,
    build_group_key,
    resolve_dimensions,
)
from public_api.aggregations.errors import (
    AggregateTimeoutError,
    AliasCollisionError,
    LimitOutOfRangeError,
    OffsetOutOfRangeError,
)
from public_api.aggregations.executor import build_aggregate_queryset
from public_api.aggregations.filters import (
    AppointmentTypeAggregateFilterInput,
    AvailableTimeAggregateFilterInput,
    BlockedTimeAggregateFilterInput,
    CalendarAggregateFilterInput,
    CalendarEventAggregateFilterInput,
    CalendarPoolAggregateFilterInput,
)
from public_api.aggregations.having import (
    AppointmentTypeHavingInput,
    AvailableTimeHavingInput,
    BlockedTimeHavingInput,
    CalendarEventHavingInput,
    CalendarHavingInput,
    CalendarPoolHavingInput,
    resolve_having,
)
from public_api.aggregations.ordering import (
    AppointmentTypeAggregateOrderInput,
    AvailableTimeAggregateOrderInput,
    BlockedTimeAggregateOrderInput,
    CalendarAggregateOrderInput,
    CalendarEventAggregateOrderInput,
    CalendarPoolAggregateOrderInput,
    resolve_order_by,
)
from public_api.aggregations.output_types import (
    ROW_TYPE_BY_ENTITY,
    AppointmentTypeAggregateRow,
    AvailableTimeAggregateRow,
    BlockedTimeAggregateRow,
    CalendarAggregateRow,
    CalendarEventAggregateRow,
    CalendarPoolAggregateRow,
)
from public_api.aggregations.plan import (
    MAX_AGGREGATE_LIMIT,
    MIN_AGGREGATE_LIMIT,
    ROW_COUNT_ALIAS,
    ROW_COUNT_FIELD_PATH,
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    FilterBounds,
    MetricSpec,
    default_metric_alias,
)
from public_api.aggregations.registry import EntityRegistration, FieldKind, get_registration
from public_api.aggregations.types import (
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
)
from public_api.constants import AGGREGATE_STATEMENT_TIMEOUT_MS
from public_api.types import PublicApiHttpRequest


#: GraphQL sub-field name to :class:`AggregateOp`, per :class:`FieldKind`.
#: Only a field's own kind's map is ever consulted, so ``sum`` on a string
#: field is simply absent rather than reachable -- the schema already
#: refused it (``StringAggregate`` has no ``sum`` field to select), and this
#: table does not need to refuse it a second time.
_NUMERIC_SELECTION_OPS: dict[str, AggregateOp] = {
    "sum": AggregateOp.SUM,
    "avg": AggregateOp.AVG,
    "min": AggregateOp.MIN,
    "max": AggregateOp.MAX,
}
_STRING_SELECTION_OPS: dict[str, AggregateOp] = {
    "min": AggregateOp.MIN,
    "max": AggregateOp.MAX,
    "concat": AggregateOp.CONCAT,
}
_DATETIME_SELECTION_OPS: dict[str, AggregateOp] = {
    "min": AggregateOp.MIN,
    "max": AggregateOp.MAX,
}
_BOOLEAN_SELECTION_OPS: dict[str, AggregateOp] = {
    "trueCount": AggregateOp.TRUE_COUNT,
    "falseCount": AggregateOp.FALSE_COUNT,
}

_SELECTION_OPS_BY_KIND: dict[FieldKind, dict[str, AggregateOp]] = {
    FieldKind.NUMERIC: _NUMERIC_SELECTION_OPS,
    FieldKind.STRING: _STRING_SELECTION_OPS,
    FieldKind.DATETIME: _DATETIME_SELECTION_OPS,
    FieldKind.BOOLEAN: _BOOLEAN_SELECTION_OPS,
}

#: The mandatory row-count metric every row carries, regardless of selection
#: -- ``*AggregateRow.count`` is non-null in every entity's output type.
_COUNT_METRIC = MetricSpec(
    alias=ROW_COUNT_ALIAS, field_path=ROW_COUNT_FIELD_PATH, op=AggregateOp.COUNT
)


def _get_organization(info: strawberry.Info) -> Organization:
    """The bound organization for this request, or a refusal.

    Deliberately not imported from ``public_api.queries``: that module
    registers these fields on ``Query``, so importing back from it would be
    circular. The check is one line and is not expected to drift.
    """
    organization = info.context.request.public_api_organization
    if not organization:
        raise GraphQLError("Organization not found in request context")
    return organization


def _validate_slice(limit: int, offset: int) -> None:
    """Reject an out-of-band ``limit`` / ``offset`` before a plan is built.

    Same wording, same order of checks, as ``public_api.queries._slice_qs``
    -- reused as text rather than by import, for the same circular-import
    reason as ``_get_organization``.
    """
    if offset < 0:
        raise OffsetOutOfRangeError()
    if not MIN_AGGREGATE_LIMIT <= limit <= MAX_AGGREGATE_LIMIT:
        raise LimitOutOfRangeError(MIN_AGGREGATE_LIMIT, MAX_AGGREGATE_LIMIT)


def _filter_bounds(filter_input: Any) -> FilterBounds:
    """The audit-safe shape of ``filter_input``.

    Only ever opaque record ids and the datetime bound, per
    ``FilterBounds``'s own contract -- never a free-text predicate such as
    ``CalendarPoolAggregateFilterInput.name_contains``.
    """
    predicates: dict[str, tuple[int, ...]] = {}
    calendar_id = getattr(filter_input, "calendar_id", None)
    if calendar_id is not None:
        predicates["calendar_id"] = (calendar_id,)
    return FilterBounds(
        start=getattr(filter_input, "start_datetime", None),
        end=getattr(filter_input, "end_datetime", None),
        predicates=predicates,
    )


def _flatten_selections(selections: Sequence[Selection]) -> list[SelectedField]:
    """Expand fragments in place, so callers only ever see concrete fields.

    A query built with ``... on X`` or a named fragment carries its fields
    under ``InlineFragment`` / ``FragmentSpread`` nodes rather than directly
    in ``selections``; both wrap another list of :class:`Selection` that may
    itself need expanding.
    """
    flat: list[SelectedField] = []
    for selection in selections:
        if isinstance(selection, SelectedField):
            flat.append(selection)
        elif isinstance(selection, (InlineFragment, FragmentSpread)):
            flat.extend(_flatten_selections(selection.selections))
    return flat


def _row_selections(info: strawberry.Info) -> list[SelectedField]:
    """The row-type field selections under this aggregate field.

    The field's own return type is a list, so GraphQL's selection set on the
    field IS the row type's selection set -- there is no intervening
    "edges/node" indirection to unwrap.
    """
    top = info.selected_fields
    if not top:
        return []
    return _flatten_selections(top[0].selections)


def _add_metric(metrics: list[MetricSpec], seen: dict[str, MetricSpec], metric: MetricSpec) -> None:
    """Add ``metric`` unless an identical one under the same alias is already present.

    GraphQL merges fields with the same response key, so the same metric can
    legitimately arrive twice -- directly, or through two overlapping
    fragments (the shape Apollo/Relay codegen emits). A byte-for-byte repeat
    (same field, same operation, same options) is silently collapsed to one,
    since it would compute the same value either way. A repeat that only
    shares the alias but differs in field, operation, or options -- the
    ``title { concat(separator: ";") }`` / ``title { concat(separator: "|") }``
    case -- cannot be collapsed without silently picking one caller's answer
    for both, so it is refused instead of being allowed to overwrite.
    """
    existing = seen.get(metric.alias)
    if existing is None:
        seen[metric.alias] = metric
        metrics.append(metric)
        return
    if existing == metric:
        return
    raise AliasCollisionError(f"Alias {metric.alias!r} is requested with more than one meaning")


def _build_metrics(
    registration: EntityRegistration, row_selections: Sequence[SelectedField]
) -> tuple[MetricSpec, ...]:
    """The metrics this call actually needs, read off the row selection.

    ``count`` is always included, whether or not the caller selected it,
    because the output type declares it non-null. Every other metric is
    computed only when its row field -- or, for a compound aggregate type,
    one of its operation sub-fields -- was actually asked for.
    """
    metrics: list[MetricSpec] = [_COUNT_METRIC]
    seen: dict[str, MetricSpec] = {_COUNT_METRIC.alias: _COUNT_METRIC}
    reverse: dict[str, str] = {}
    for key in registration.metrics:
        reverse[_to_camel_case(key)] = key
    for key in registration.relation_counts:
        reverse[_to_camel_case(key)] = key

    for selection in row_selections:
        if selection.name in ("key", "count"):
            continue
        field_path = reverse.get(selection.name)
        if field_path is None:
            continue

        if field_path in registration.relation_counts:
            _add_metric(
                metrics,
                seen,
                MetricSpec(alias=field_path, field_path=field_path, op=AggregateOp.COUNT),
            )
            continue

        aggregatable = registration.metrics[field_path]
        op_by_name = _SELECTION_OPS_BY_KIND[aggregatable.kind]
        for sub_selection in _flatten_selections(selection.selections):
            op = op_by_name.get(sub_selection.name)
            if op is None:
                continue
            options: dict[str, Any] = {}
            if op is AggregateOp.CONCAT:
                options["separator"] = sub_selection.arguments.get("separator", ",")
                options["distinct"] = sub_selection.arguments.get("distinct", False)
            alias = default_metric_alias(field_path, op)
            _add_metric(
                metrics,
                seen,
                MetricSpec(alias=alias, field_path=field_path, op=op, options=options),
            )

    return tuple(metrics)


def _to_camel_case(name: str) -> str:
    """The GraphQL field name Strawberry exposes ``name`` as.

    Matches Strawberry's own conversion exactly (first word lowercase, every
    later word capitalized) so a lookup built from it agrees with what a
    caller's selection actually names.
    """
    first, *rest = name.split("_")
    return first + "".join(word.capitalize() for word in rest)


def _build_aggregate_value(kind: FieldKind, values: dict[AggregateOp, Any]) -> Any:
    """One field's aggregate sub-object, built from its requested operations.

    An operation that was not requested is simply absent from ``values``,
    which leaves that slot at the aggregate type's own ``None`` default --
    the honest "not asked for", same as "asked for and NULL in the data".
    """
    if kind is FieldKind.NUMERIC:
        return NumericAggregate(
            sum=values.get(AggregateOp.SUM),
            avg=values.get(AggregateOp.AVG),
            min=values.get(AggregateOp.MIN),
            max=values.get(AggregateOp.MAX),
        )
    if kind is FieldKind.STRING:
        return StringAggregate(
            min=values.get(AggregateOp.MIN),
            max=values.get(AggregateOp.MAX),
            concat_value=values.get(AggregateOp.CONCAT),
        )
    if kind is FieldKind.DATETIME:
        return DateTimeAggregate(
            min=values.get(AggregateOp.MIN),
            max=values.get(AggregateOp.MAX),
        )
    if kind is FieldKind.BOOLEAN:
        return BooleanAggregate(
            true_count=values.get(AggregateOp.TRUE_COUNT, 0),
            false_count=values.get(AggregateOp.FALSE_COUNT, 0),
        )
    raise AssertionError(f"Unhandled field kind {kind!r}")  # pragma: no cover


def _row_to_output(
    entity: AggregatableEntity,
    registration: EntityRegistration,
    row: dict[str, Any],
    metrics: tuple[MetricSpec, ...],
) -> Any:
    """One executor row dict, mapped onto the entity's ``*AggregateRow`` type."""
    row_type = ROW_TYPE_BY_ENTITY[entity]
    kwargs: dict[str, Any] = {
        "key": build_group_key(entity, row),
        "count": row.get(ROW_COUNT_ALIAS, 0),
    }

    grouped: dict[str, dict[AggregateOp, Any]] = {}
    for metric in metrics:
        if metric.alias == ROW_COUNT_ALIAS:
            continue
        if metric.field_path in registration.relation_counts:
            kwargs[metric.field_path] = row.get(metric.alias)
            continue
        grouped.setdefault(metric.field_path, {})[metric.op] = row.get(metric.alias)

    for field_path, values in grouped.items():
        aggregatable = registration.metrics[field_path]
        kwargs[field_path] = _build_aggregate_value(aggregatable.kind, values)

    return row_type(**kwargs)


def _is_statement_timeout(exc: OperationalError) -> bool:
    """Whether ``exc`` is Postgres cancelling a query for exceeding its budget.

    Django re-raises psycopg's error as its own ``OperationalError`` with the
    original as ``__cause__`` -- see ``common.testing.migration_replay``'s
    ``_is_deadlock`` for the exact precedent. SQLSTATE 57014
    (``QueryCanceled``) is what a ``statement_timeout`` cancellation raises.
    """
    return isinstance(exc.__cause__, QueryCanceled)


def _execute_with_statement_timeout(
    queryset: Any,
) -> list[dict[str, Any]]:
    """Evaluate ``queryset`` under a Postgres statement timeout scoped to it alone.

    ``ATOMIC_REQUESTS`` already wraps the whole request in one transaction,
    and ``SET LOCAL`` only reverts at the end of a transaction or on a
    savepoint rollback -- never on a plain savepoint release. So the nested
    ``transaction.atomic()`` savepoint here does two jobs: on the timeout
    path, letting the query's ``OperationalError`` propagate rolls the
    savepoint back, which is what undoes ``SET LOCAL`` and leaves the
    request's outer transaction usable for whatever the resolver does next;
    on the success path, the explicit reset before returning is what stops
    the timeout leaking into the rest of the request once the savepoint is
    released rather than rolled back.
    """
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = %s", [AGGREGATE_STATEMENT_TIMEOUT_MS])
            rows = list(queryset)
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = DEFAULT")
            return rows
    except OperationalError as exc:
        if _is_statement_timeout(exc):
            raise AggregateTimeoutError() from None
        raise


def _execute_aggregate(
    entity: AggregatableEntity,
    info: strawberry.Info,
    filter_input: Any,
    group_by: Sequence[Any],
    timezone: str,
    limit: int,
    offset: int,
    having: Any,
    order_by: Sequence[Any] | None,
) -> list[Any]:
    """The one resolver body every aggregate root field shares."""
    _validate_slice(limit, offset)

    organization = _get_organization(info)
    request: PublicApiHttpRequest = info.context.request
    system_user = request.public_api_system_user

    base_queryset = filter_input.apply(system_user, organization)
    dimensions = resolve_dimensions(group_by, timezone)

    registration = get_registration(entity)

    metrics_list: list[MetricSpec] = []
    seen: dict[str, MetricSpec] = {}
    for metric in _build_metrics(registration, _row_selections(info)):
        _add_metric(metrics_list, seen, metric)

    having_spec, having_metrics = resolve_having(having)
    for metric in having_metrics:
        _add_metric(metrics_list, seen, metric)

    dimension_aliases = tuple(dimension.alias for dimension in dimensions)
    order_specs, order_metrics = resolve_order_by(
        entity, order_by or (), timezone, dimension_aliases
    )
    for metric in order_metrics:
        _add_metric(metrics_list, seen, metric)

    metrics = tuple(metrics_list)

    plan = AggregateQueryPlan(
        entity=entity,
        dimensions=dimensions,
        metrics=metrics,
        filter_bounds=_filter_bounds(filter_input),
        having=having_spec,
        order_by=order_specs,
        limit=limit,
        offset=offset,
    )

    queryset = build_aggregate_queryset(plan, base_queryset)
    rows = _execute_with_statement_timeout(queryset)

    return [_row_to_output(entity, registration, row, metrics) for row in rows]


# ---------------------------------------------------------------------------
# The six root fields
# ---------------------------------------------------------------------------
#
# Each wrapper's only job is to give Strawberry a concrete signature for one
# entity; the body is always the single call into ``_execute_aggregate``
# above. ``filter`` is the GraphQL argument name the API design fixes (see
# "Filter inputs are objects" in the plan's Guiding Decisions) but shadows
# the ``filter`` builtin, so the Python parameter is named ``filter_input``
# and renamed back for the schema via ``strawberry.argument``.


def calendar_event_aggregate(
    info: strawberry.Info,
    filter_input: Annotated[CalendarEventAggregateFilterInput, strawberry.argument(name="filter")],
    group_by: list[CalendarEventGroupByInput],
    timezone: str,
    limit: int = MAX_AGGREGATE_LIMIT,
    offset: int = 0,
    having: CalendarEventHavingInput | None = None,
    order_by: list[CalendarEventAggregateOrderInput] | None = None,
) -> list[CalendarEventAggregateRow]:
    """Group and aggregate ``CalendarEvent`` rows visible to the caller's token."""
    return _execute_aggregate(
        AggregatableEntity.CALENDAR_EVENT,
        info,
        filter_input,
        group_by,
        timezone,
        limit,
        offset,
        having,
        order_by,
    )


def available_time_aggregate(
    info: strawberry.Info,
    filter_input: Annotated[AvailableTimeAggregateFilterInput, strawberry.argument(name="filter")],
    group_by: list[AvailableTimeGroupByInput],
    timezone: str,
    limit: int = MAX_AGGREGATE_LIMIT,
    offset: int = 0,
    having: AvailableTimeHavingInput | None = None,
    order_by: list[AvailableTimeAggregateOrderInput] | None = None,
) -> list[AvailableTimeAggregateRow]:
    """Group and aggregate ``AvailableTime`` rows visible to the caller's token."""
    return _execute_aggregate(
        AggregatableEntity.AVAILABLE_TIME,
        info,
        filter_input,
        group_by,
        timezone,
        limit,
        offset,
        having,
        order_by,
    )


def blocked_time_aggregate(
    info: strawberry.Info,
    filter_input: Annotated[BlockedTimeAggregateFilterInput, strawberry.argument(name="filter")],
    group_by: list[BlockedTimeGroupByInput],
    timezone: str,
    limit: int = MAX_AGGREGATE_LIMIT,
    offset: int = 0,
    having: BlockedTimeHavingInput | None = None,
    order_by: list[BlockedTimeAggregateOrderInput] | None = None,
) -> list[BlockedTimeAggregateRow]:
    """Group and aggregate ``BlockedTime`` rows visible to the caller's token."""
    return _execute_aggregate(
        AggregatableEntity.BLOCKED_TIME,
        info,
        filter_input,
        group_by,
        timezone,
        limit,
        offset,
        having,
        order_by,
    )


def appointment_type_aggregate(
    info: strawberry.Info,
    filter_input: Annotated[
        AppointmentTypeAggregateFilterInput, strawberry.argument(name="filter")
    ],
    group_by: list[AppointmentTypeGroupByInput],
    timezone: str,
    limit: int = MAX_AGGREGATE_LIMIT,
    offset: int = 0,
    having: AppointmentTypeHavingInput | None = None,
    order_by: list[AppointmentTypeAggregateOrderInput] | None = None,
) -> list[AppointmentTypeAggregateRow]:
    """Group and aggregate ``AppointmentType`` rows visible to the caller's token."""
    return _execute_aggregate(
        AggregatableEntity.APPOINTMENT_TYPE,
        info,
        filter_input,
        group_by,
        timezone,
        limit,
        offset,
        having,
        order_by,
    )


def calendar_aggregate(
    info: strawberry.Info,
    filter_input: Annotated[CalendarAggregateFilterInput, strawberry.argument(name="filter")],
    group_by: list[CalendarGroupByInput],
    timezone: str,
    limit: int = MAX_AGGREGATE_LIMIT,
    offset: int = 0,
    having: CalendarHavingInput | None = None,
    order_by: list[CalendarAggregateOrderInput] | None = None,
) -> list[CalendarAggregateRow]:
    """Group and aggregate ``Calendar`` rows visible to the caller's token."""
    return _execute_aggregate(
        AggregatableEntity.CALENDAR,
        info,
        filter_input,
        group_by,
        timezone,
        limit,
        offset,
        having,
        order_by,
    )


def calendar_pool_aggregate(
    info: strawberry.Info,
    filter_input: Annotated[CalendarPoolAggregateFilterInput, strawberry.argument(name="filter")],
    group_by: list[CalendarPoolGroupByInput],
    timezone: str,
    limit: int = MAX_AGGREGATE_LIMIT,
    offset: int = 0,
    having: CalendarPoolHavingInput | None = None,
    order_by: list[CalendarPoolAggregateOrderInput] | None = None,
) -> list[CalendarPoolAggregateRow]:
    """Group and aggregate ``CalendarPool`` rows visible to the caller's token."""
    return _execute_aggregate(
        AggregatableEntity.CALENDAR_POOL,
        info,
        filter_input,
        group_by,
        timezone,
        limit,
        offset,
        having,
        order_by,
    )
