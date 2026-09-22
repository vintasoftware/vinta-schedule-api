"""One grouped query per level, however many parents are on that level.

An aggregate is reachable under a parent object as well as at the root --
``calendars { eventAggregate { ... } }``. Resolved the obvious way that is one
grouped query per calendar, and nothing in the stack would batch it:
``DjangoOptimizerExtension`` batches ``select_related`` / ``prefetch_related``,
which are relation fetches, and a ``GROUP BY`` is neither. Twenty-five
calendars would be twenty-five queries, and the failure would look like
correct numbers plus database load.

So the parent key is folded into the ``GROUP BY`` instead. The first nested
resolver to run at a level executes **one** query grouped by
``<parent key> + <the caller's dimensions>``, covering every parent at that
level at once; the rows are bucketed by parent key and cached on the request;
every sibling resolver after it reads its own bucket out of the cache. The
query count for a level is one, whether the level holds five parents or a
hundred.

**What a "level" is.** The GraphQL path with its list indices dropped --
``calendars.eventAggregate`` -- paired with the resolved plan. The path is
what makes two *different* nested fields (two aliases of ``eventAggregate``
with different filters, say) separate batches, and the plan is what makes a
cached batch honest about what it holds. Both are needed: the path alone
would merge a level that appears twice under different arguments, and the plan
alone cannot see a filter predicate it does not record.

**Why the parent set is never collected.** A sync GraphQL execution resolves
each list item in turn, so the first sibling to run cannot see the ones after
it -- there is no point at which the parent ids are all known and no query has
run yet. The batch therefore covers every row the token can already see
matching the filter, grouped by parent, and a parent that asked for nothing is
simply never looked up. That discloses nothing new: the base queryset is the
same one ``public_api.aggregations.filters`` builds for the root-level field,
so the batch is exactly the root-level aggregate grouped by one more column.

**What that costs, and what bounds it.** The batch covers every parent in the
organization at that level, *not* the parents on the page being rendered:
``calendars(limit: 5) { eventAggregate }`` against an organization holding ten
thousand calendars groups all ten thousand, and 9,995 of the buckets are
thrown away. The per-parent ``LIMIT`` rides in the same statement as a
``ROW_NUMBER`` window (see ``executor._sliced_per_parent``), which bounds the
groups *per parent* and bounds nothing about their product. So the product is
bounded separately, by ``MAX_BATCHED_AGGREGATE_ROWS`` in
``public_api.constants``: ``fields._batched_rows`` asks for one row more than
the cap and raises ``BatchTooLargeError`` if it arrives. Refusing rather than
truncating is the point -- rows arrive parent by parent, so dropping the tail
would hand the last parents on the level an empty list, which reads as "this
calendar has no events" rather than as a limit being hit. Bounding by the
page's own parent ids would be tighter and is not available for the reason the
paragraph above gives.

**Interaction with ``DjangoOptimizerExtension``.** None, deliberately. The
collector never touches the parent queryset, never adds a ``select_related``
or ``prefetch_related`` hint, and never re-fetches a parent: it reads the
parent's primary key off the model instance the optimizer already produced and
looks it up in a dict. The optimizer's own hints on the parent list are
therefore untouched, and the extension has nothing to undo. The query-count
test in ``public_api/tests/aggregations/test_nested_batching.py`` is what holds
that to account.
"""

from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import strawberry

from public_api.aggregations.plan import AggregatableEntity, AggregateQueryPlan


@dataclass(frozen=True)
class NestedAggregateLink:
    """How one nested aggregate field reaches its parent.

    ``parent_field_path`` is a path on the *aggregated* model that arrives at
    the parent's primary key. For a calendar's events that is the concrete
    foreign key column; for a pool's events it traverses the calendar and the
    pool's through table, because a ``CalendarEvent`` carries no pool of its
    own.

    The four instances below are the whole set the schema exposes. A caller
    cannot name one, which is why the path is allowed to be a join rather than
    a registered dimension -- see
    :class:`~public_api.aggregations.plan.ParentKeySpec`.
    """

    entity: AggregatableEntity
    parent_field_path: str


#: A calendar's own events and blocked times, by the concrete foreign key on
#: each. Named through ``calendar_fk_id`` rather than the safe relation for the
#: same reason the registry's dimensions are: the value is on the row being
#: grouped, so there is no ``ON`` clause for the organization to fall out of,
#: and tenant scoping is in the base queryset's ``WHERE`` where it belongs.
CALENDAR_EVENTS = NestedAggregateLink(AggregatableEntity.CALENDAR_EVENT, "calendar_fk_id")
CALENDAR_BLOCKED_TIMES = NestedAggregateLink(AggregatableEntity.BLOCKED_TIME, "calendar_fk_id")

#: An appointment type's events, by the event's own appointment-type column.
APPOINTMENT_TYPE_EVENTS = NestedAggregateLink(
    AggregatableEntity.CALENDAR_EVENT, "appointment_type_fk_id"
)

#: A pool's events: every event on a calendar the pool rosters. The traversal
#: goes through the organization-safe ``calendar`` relation and the reverse of
#: ``CalendarPoolMembership.calendar``, so the organization stays in both
#: ``ON`` clauses. A calendar in two pools contributes its events to both,
#: which is the answer the question asks for, and the roster's unique
#: constraint on ``(pool, calendar)`` is what stops one pool counting an event
#: twice.
CALENDAR_POOL_EVENTS = NestedAggregateLink(
    AggregatableEntity.CALENDAR_EVENT, "calendar__pool_memberships__pool_fk_id"
)


def level_key(info: strawberry.Info) -> tuple[str, ...]:
    """This field's GraphQL path with the list indices dropped.

    ``calendars.3.eventAggregate`` and ``calendars.17.eventAggregate`` are the
    same *level* -- one field node, resolved once per parent -- and this is
    what says so. An aliased second selection of the same field has its own
    response key, so it gets its own level and its own batch.
    """
    parts: list[str] = []
    path: Any = info.path
    while path is not None:
        if isinstance(path.key, str):
            parts.append(path.key)
        path = path.prev
    parts.reverse()
    return tuple(parts)


def batch_key(level: tuple[str, ...], plan: AggregateQueryPlan) -> Hashable:
    """The cache key one batched query is stored under.

    Both halves earn their place: two levels that resolve the same plan are
    still two different field nodes and may carry filter arguments the plan
    does not record, and one level asked for twice in one document is one
    query rather than two.
    """
    return (level, plan)


def group_rows_by_parent(
    rows: Iterable[Mapping[str, Any]], parent_alias: str
) -> dict[Any, list[dict[str, Any]]]:
    """Bucket one batched query's rows by the parent each belongs to.

    Row order is preserved inside a bucket, so the ordering the query applied
    within a parent is the order that parent's page comes back in. A row whose
    parent key is ``NULL`` -- an event on a calendar in no pool, reached
    through the pool link's outer join -- belongs to no parent and is dropped.
    """
    buckets: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        parent_id = row.get(parent_alias)
        if parent_id is None:
            continue
        buckets.setdefault(parent_id, []).append(dict(row))
    return buckets


class NestedAggregateCollector:
    """One request's batched grouped results, one entry per level and plan.

    Deliberately not a cache with eviction or a TTL: it lives exactly as long
    as the request that built it, and a GraphQL document's levels are bounded
    by the schema's own depth limit.
    """

    def __init__(self) -> None:
        self._batches: dict[Hashable, dict[Any, list[dict[str, Any]]]] = {}

    def rows_for(
        self,
        key: Hashable,
        parent_id: Any,
        execute: Callable[[], dict[Any, list[dict[str, Any]]]],
    ) -> list[dict[str, Any]]:
        """This parent's rows, running ``execute`` once per level.

        A parent with no matching rows gets an empty list rather than a
        second query: the batch already read every group at this level, so
        "absent from the batch" is the answer, not a cache miss.
        """
        batch = self._batches.get(key)
        if batch is None:
            batch = execute()
            self._batches[key] = batch
        return batch.get(parent_id, [])

    @property
    def batch_count(self) -> int:
        """How many batched queries this request has run. Read by tests."""
        return len(self._batches)


#: Where the collector hangs off the request. One underscore-prefixed
#: attribute rather than a module-level dict keyed by request, so it cannot
#: outlive the request or leak one tenant's rows into another's.
_COLLECTOR_ATTRIBUTE = "_nested_aggregate_collector"


def collector_for(request: Any) -> NestedAggregateCollector:
    """The request's collector, created on first use."""
    collector = getattr(request, _COLLECTOR_ATTRIBUTE, None)
    if collector is None:
        collector = NestedAggregateCollector()
        setattr(request, _COLLECTOR_ATTRIBUTE, collector)
    return collector
