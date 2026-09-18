"""Aggregates selected under a parent, executed one query per level.

A partner asking for twenty-five calendars and each one's event rollup is
asking for one number per calendar, not twenty-five queries. Resolved literally
that is exactly what it would be: GraphQL calls a nested field's resolver once
per parent row, and ``DjangoOptimizerExtension`` -- which batches
``select_related`` and ``prefetch_related`` -- has nothing to say about a
``GROUP BY``. The N+1 that produces is the quiet kind: every functional
assertion passes, every number is right, and the only symptom is database load
in production.

**What this module does instead.** The parent column joins the aggregate's
``GROUP BY``, so one grouped query answers for every parent at that level, and
its rows are dispatched back to the parent that asked for them. The extra
column is the executor's
:class:`~public_api.aggregations.plan.ParentKeySpec`; everything else -- the
plan, the metrics read off the selection, the filter input, all four cost
guards -- is the same code the root fields run, reached through
:func:`~public_api.aggregations.fields.build_aggregate_request`. A nested
aggregate and the equivalent root aggregate grouped by the same key are the
same SQL with the same numbers, which is the only property that makes batching
safe to do invisibly.

**How the batch learns its parents.** The public API's GraphQL view is the
*synchronous* one, so Strawberry's ``DataLoader`` -- which needs an event loop
to coalesce a tick's worth of keys -- is not available, and a resolver cannot
see its own siblings. The parent set therefore has to arrive from above:
:class:`NestedAggregateExtension` watches field resolution, and when a field
whose selection contains a nested aggregate returns a list of parents it
records them against that field's response path. The first nested resolver to
fire under that path builds the whole batch from the recorded list; the other
twenty-four read their rows out of it.

**Why that does not fight the optimizer.** The extension is registered *after*
``DjangoOptimizerExtension`` in ``public_api/schema.py``, which makes it the
outer middleware: by the time it sees a queryset the optimizer has already
attached its ``only`` / ``select_related`` / ``prefetch_related`` hints and
fetched it. Recording the parents reads that fetched result and issues nothing.
Registered the other way round it would evaluate the queryset first and the
optimizer would find nothing left to optimize -- so the order in `schema.py` is
load-bearing, and ``test_nested_batching.py`` asserts the query count that would
change if it were reversed.

**A parent that is not a list** -- ``calendarPool(poolId: 3) { eventAggregate }``
-- records nothing, and the collector falls back to a batch of one. That is the
same query shape with one id in it, not a different path.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from django.db import models
from django.db.models import QuerySet

import strawberry
import strawberry_django
from graphql import GraphQLResolveInfo
from graphql.language import FieldNode, FragmentSpreadNode, InlineFragmentNode, SelectionSetNode
from strawberry.extensions import SchemaExtension
from strawberry.utils.str_converters import to_camel_case

from calendar_integration.models import AppointmentType, Calendar, CalendarPool
from public_api.aggregations.dimensions import GROUP_BY_INPUT_TYPE_BY_ENTITY
from public_api.aggregations.errors import UnknownNestedAggregateError
from public_api.aggregations.fields import (
    AGGREGATE_RESOURCE_BY_ENTITY,
    AGGREGATE_ROW_TYPE_BY_ENTITY,
    FILTER_INPUT_TYPE_BY_ENTITY,
    build_aggregate_request,
    build_rows,
    execute_request,
)
from public_api.aggregations.having import HAVING_INPUT_TYPE_BY_ENTITY
from public_api.aggregations.ordering import ORDER_INPUT_TYPE_BY_ENTITY
from public_api.aggregations.plan import MAX_LIMIT, AggregatableEntity, ParentKeySpec
from public_api.aggregations.windows import WINDOW_INPUT_TYPE_BY_ENTITY
from public_api.permissions import IsAuthenticated, OrganizationResourceAccess


# The row key the parent's own id lands under. Internal: it is grouped on and
# dispatched on, and never reaches a response. Named without a leading
# underscore because Django annotation aliases are compared against the model's
# column names, and a name that reads as a column is easier to recognise in a
# query plan than one that reads as a private attribute.
PARENT_KEY_ALIAS = "aggregate_parent_key"

# Where the collector lives for the duration of one request. The request object
# is already this API's per-request carrier -- `public_api_system_user` and
# `public_api_organization` are set on it by `PublicApiSystemUserMiddleware` --
# so a batch cache belongs there rather than in a module-level global, which two
# concurrent requests would share.
_COLLECTOR_ATTRIBUTE = "_public_api_nested_aggregate_collector"


@dataclass(frozen=True)
class NestedAggregateSpec:
    """One nested aggregate field: what it aggregates, and under which parent.

    ``parent_key_path`` is an ORM path *on the aggregated model* that resolves
    to the parent's key column. For a direct foreign key that is the concrete
    ``<name>_fk`` column, which is read rather than joined -- the established
    shape in this package, and the reason the organization cannot fall out of a
    join that is not there. For a parent reached through a relation it traverses
    the organization-safe relation names, so every join on the way carries the
    organization in its ``ON`` clause.
    """

    entity: AggregatableEntity
    parent_model: type[models.Model]
    attribute_name: str
    parent_key_path: str
    description: str

    @property
    def field_name(self) -> str:
        """The GraphQL field name, which is what the permission check reads."""
        return to_camel_case(self.attribute_name)

    @property
    def parent_key(self) -> ParentKeySpec:
        """The batching key this field folds into the ``GROUP BY``."""
        return ParentKeySpec(alias=PARENT_KEY_ALIAS, field_path=self.parent_key_path)


NESTED_AGGREGATES: tuple[NestedAggregateSpec, ...] = (
    NestedAggregateSpec(
        entity=AggregatableEntity.CALENDAR_EVENT,
        parent_model=Calendar,
        attribute_name="event_aggregate",
        parent_key_path="calendar_fk",
        description="Group and aggregate this calendar's events.",
    ),
    NestedAggregateSpec(
        entity=AggregatableEntity.BLOCKED_TIME,
        parent_model=Calendar,
        attribute_name="blocked_time_aggregate",
        parent_key_path="calendar_fk",
        description="Group and aggregate this calendar's blocked times.",
    ),
    NestedAggregateSpec(
        entity=AggregatableEntity.CALENDAR_EVENT,
        parent_model=CalendarPool,
        attribute_name="event_aggregate",
        # Through the pool's membership rows: an event belongs to a pool when
        # its calendar does. Both hops name the organization-safe relation, so
        # each join matches on organization as well as on the key.
        parent_key_path="calendar__pool_memberships__pool_fk",
        description="Group and aggregate events on the calendars in this pool.",
    ),
    NestedAggregateSpec(
        entity=AggregatableEntity.CALENDAR_EVENT,
        parent_model=AppointmentType,
        attribute_name="event_aggregate",
        parent_key_path="appointment_type_fk",
        description="Group and aggregate this appointment type's events.",
    ),
)

_SPEC_BY_PARENT_FIELD: Mapping[tuple[type[models.Model], str], NestedAggregateSpec] = (
    MappingProxyType({(spec.parent_model, spec.attribute_name): spec for spec in NESTED_AGGREGATES})
)


def nested_aggregate_spec(
    parent_model: type[models.Model], attribute_name: str
) -> NestedAggregateSpec:
    """The registration for one parent type's nested aggregate field.

    Looked up rather than written out at the GraphQL type, so the parent key
    path and the entity live in one place with the collector that uses them and
    a field cannot be declared this module knows nothing about.
    """
    try:
        return _SPEC_BY_PARENT_FIELD[(parent_model, attribute_name)]
    except KeyError:
        raise UnknownNestedAggregateError(parent_model.__name__, attribute_name) from None


# The GraphQL field names a nested aggregate can appear under. Read by the
# extension to decide whether a field's result is worth recording, so it is a
# set rather than a scan of `NESTED_AGGREGATES`.
NESTED_FIELD_NAMES: frozenset[str] = frozenset(spec.field_name for spec in NESTED_AGGREGATES)

# Which resource each nested field requires: the *aggregated entity's*, never
# the parent's. Nesting an event aggregate under a calendar is still a read of
# events, so a calendar-only token is refused it. The entries these names need
# in `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING` are written out
# there and asserted against this mapping in
# `public_api/tests/aggregations/test_nested_permissions.py` -- written out
# rather than imported, because `nested.py` imports the permission classes and
# the reverse would be a cycle.
NESTED_RESOURCE_BY_FIELD_NAME: Mapping[str, str] = MappingProxyType(
    {spec.field_name: AGGREGATE_RESOURCE_BY_ENTITY[spec.entity] for spec in NESTED_AGGREGATES}
)


# ---------------------------------------------------------------------------
# Response paths
# ---------------------------------------------------------------------------


def response_path(info: GraphQLResolveInfo | strawberry.Info) -> tuple[str, ...]:
    """A field's response path with the list indices dropped.

    ``calendars.0.eventAggregate`` and ``calendars.24.eventAggregate`` are the
    same *selection* resolving against different rows, so dropping the index is
    what makes them one batch. Two aliases of the same field keep different
    paths and stay separate batches, which is right: they may carry different
    arguments.
    """
    raw = getattr(info, "path", None) or getattr(getattr(info, "_raw_info", None), "path", None)
    parts: list[str] = []
    while raw is not None:
        if isinstance(raw.key, str):
            parts.append(raw.key)
        raw = raw.prev
    parts.reverse()
    return tuple(parts)


# ---------------------------------------------------------------------------
# The collector
# ---------------------------------------------------------------------------


class NestedAggregateCollector:
    """One request's nested aggregates: the parents seen, the batches run.

    Keyed by response path throughout. A parent list records itself under its
    own path (``("calendars",)``); a nested aggregate under it asks for the
    batch at its path (``("calendars", "eventAggregate")``) and finds its
    parents one level up.
    """

    def __init__(self) -> None:
        self._parents: dict[tuple[str, ...], list[models.Model]] = {}
        self._batches: dict[tuple[str, ...], dict[Any, list[Any]]] = {}

    # -- recording -------------------------------------------------------

    def record_parents(self, path: tuple[str, ...], parents: Sequence[models.Model]) -> None:
        """Remember the rows a parent list resolved to, for the batch below it.

        Recorded once per path: a field resolves once, and a second call would
        mean a different list under the same response key, which cannot happen.
        """
        self._parents.setdefault(path, list(parents))

    # -- resolving -------------------------------------------------------

    def rows_for(
        self,
        spec: NestedAggregateSpec,
        root: models.Model,
        info: strawberry.Info,
        arguments: Mapping[str, Any],
    ) -> list[Any]:
        """This parent's rows, running the batch's single query if it has not run.

        The first parent under a given path pays for the whole level; the rest
        read out of the dictionary it left behind. A parent with no matching
        rows gets an empty list, which is what the same aggregate returns at the
        root: buckets are sparse, so "no rows" and "no groups" are the same
        answer.
        """
        path = response_path(info)
        batch = self._batches.get(path)
        if batch is None:
            batch = self._run_batch(spec, root, info, arguments, path)
            self._batches[path] = batch
        return batch.get(root.pk, [])

    def _run_batch(
        self,
        spec: NestedAggregateSpec,
        root: models.Model,
        info: strawberry.Info,
        arguments: Mapping[str, Any],
        path: tuple[str, ...],
    ) -> dict[Any, list[Any]]:
        """Execute one level's aggregate and split its rows by parent."""
        parents = self._parents.get(path[:-1]) or [root]
        parent_ids = [parent.pk for parent in parents]

        request = build_aggregate_request(
            spec.entity,
            info,
            arguments["filter"],
            arguments["group_by"],
            arguments["timezone"],
            arguments["having"],
            arguments["order_by"],
            arguments["window"],
            arguments["limit"],
            arguments["offset"],
            parent_key=spec.parent_key,
        )
        # Narrowing to this level's parents, on top of everything the filter
        # input already narrowed: the queryset it built is organization-scoped
        # and owner-scoped, and restricting it to ids drawn from parents the
        # same request already returned can only make it smaller.
        request = request.narrowed(
            request.queryset.filter(**{f"{spec.parent_key_path}__in": parent_ids})
        )

        rows_by_parent: dict[Any, list[Mapping[str, Any]]] = {}
        for row in execute_request(request):
            rows_by_parent.setdefault(row[PARENT_KEY_ALIAS], []).append(row)

        return {parent_id: build_rows(request, rows) for parent_id, rows in rows_by_parent.items()}


def collector_for(info: strawberry.Info | GraphQLResolveInfo) -> NestedAggregateCollector:
    """The request's collector, created on first use."""
    request = info.context.request
    collector = getattr(request, _COLLECTOR_ATTRIBUTE, None)
    if collector is None:
        collector = NestedAggregateCollector()
        setattr(request, _COLLECTOR_ATTRIBUTE, collector)
    return collector


# ---------------------------------------------------------------------------
# The extension that feeds it
# ---------------------------------------------------------------------------


def _selected_field_names(
    selection_set: SelectionSetNode | None, fragments: Mapping[str, Any]
) -> Iterator[str]:
    """The field names a selection set asks for, seeing through fragments.

    A partner may perfectly well put its nested aggregate behind a named
    fragment, and a batch that only fires for inline selections would be a
    silent N+1 for the half of the documents that use one.
    """
    if selection_set is None:
        return
    for selection in selection_set.selections:
        if isinstance(selection, FieldNode):
            yield selection.name.value
        elif isinstance(selection, InlineFragmentNode):
            yield from _selected_field_names(selection.selection_set, fragments)
        elif isinstance(selection, FragmentSpreadNode):
            fragment = fragments.get(selection.name.value)
            if fragment is not None:
                yield from _selected_field_names(fragment.selection_set, fragments)


class NestedAggregateExtension(SchemaExtension):
    """Records the parent rows a nested aggregate is about to be asked for.

    Must be registered **after** ``DjangoOptimizerExtension``: graphql-core
    chains middleware so that the last one listed is the outermost, and this one
    has to see a queryset the optimizer has already hinted and fetched. See this
    module's docstring.

    It touches nothing. The value the resolver returned is the value it returns.
    """

    def resolve(
        self,
        _next: Any,
        root: Any,
        info: GraphQLResolveInfo,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        result = _next(root, info, *args, **kwargs)

        field_nodes = info.field_nodes
        if not field_nodes:
            return result
        selected = _selected_field_names(field_nodes[0].selection_set, info.fragments)
        if not any(name in NESTED_FIELD_NAMES for name in selected):
            # The overwhelming majority of fields, and the whole cost of this
            # extension for a document that asks for no nested aggregate.
            return result

        parents = _as_model_rows(result)
        if parents is not None:
            collector_for(info).record_parents(response_path(info), parents)
        return result


def _as_model_rows(value: Any) -> list[models.Model] | None:
    """``value`` as a list of model rows, or ``None`` if it is not one.

    A ``QuerySet`` reaching here has already been fetched by the optimizer, so
    listing it reads the result cache rather than issuing a query. It is listed
    rather than left alone because the optimizer can be disabled, and a batch
    that quietly stops batching when it is would be an N+1 nobody is measuring.
    """
    if isinstance(value, QuerySet):
        value = list(value)
    if not isinstance(value, list | tuple):
        return None
    rows = [item for item in value if isinstance(item, models.Model)]
    return rows or None


# ---------------------------------------------------------------------------
# The field factory
# ---------------------------------------------------------------------------


def nested_aggregate_field(spec: NestedAggregateSpec) -> Any:
    """Build the aggregate field ``spec`` describes, for its parent type.

    The same arguments the root field takes, resolved by the same code, gated by
    the *aggregated* entity's resource rather than the parent's -- reading a
    calendar is not reading its events.
    """
    entity = spec.entity
    filter_type = FILTER_INPUT_TYPE_BY_ENTITY[entity]
    group_by_type = GROUP_BY_INPUT_TYPE_BY_ENTITY[entity]
    having_type = HAVING_INPUT_TYPE_BY_ENTITY[entity]
    order_type = ORDER_INPUT_TYPE_BY_ENTITY[entity]
    window_type = WINDOW_INPUT_TYPE_BY_ENTITY[entity]
    row_type = AGGREGATE_ROW_TYPE_BY_ENTITY[entity]

    def resolver(
        root: Any,
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
        return collector_for(info).rows_for(
            spec,
            root,
            info,
            {
                "filter": filter,
                "group_by": group_by,
                "timezone": timezone,
                "having": having,
                "order_by": order_by,
                "window": window,
                "limit": limit,
                "offset": offset,
            },
        )

    resolver.__name__ = spec.attribute_name
    resolver.__qualname__ = resolver.__name__
    resolver.__annotations__ = {
        "root": spec.parent_model,
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
            f"{spec.description} Takes the same arguments as the root aggregate "
            f"field and returns the same rows it would for this parent alone -- "
            f"one query answers for every parent at this level, so selecting it "
            f"under a list costs the same as selecting it under one."
        ),
    )
