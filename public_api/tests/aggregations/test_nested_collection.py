"""The per-level collector, below the schema.

Three things it has to get right, and each is a wrong answer rather than an
error if it does not:

* what counts as one *level* -- the GraphQL path with its list indices
  dropped, so twenty-five parents share a batch and two aliased selections do
  not;
* one batched query per level and plan, and no second query for a parent the
  batch found nothing for;
* dispatching each row to the parent it belongs to, in the order the query
  returned it.
"""

import pytest
from graphql.pyutils import Path

from public_api.aggregations.nested import (
    APPOINTMENT_TYPE_EVENTS,
    CALENDAR_BLOCKED_TIMES,
    CALENDAR_EVENTS,
    CALENDAR_POOL_EVENTS,
    NestedAggregateCollector,
    batch_key,
    collector_for,
    group_rows_by_parent,
    level_key,
)
from public_api.aggregations.plan import (
    PARENT_KEY_ALIAS,
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    DimensionSpec,
    MetricSpec,
    ParentKeySpec,
)


def _plan(dimensions: tuple[DimensionSpec, ...] | None = None) -> AggregateQueryPlan:
    """A minimal nested plan, with the parent key every nested plan carries."""
    return AggregateQueryPlan(
        entity=AggregatableEntity.CALENDAR_EVENT,
        dimensions=dimensions or (DimensionSpec(alias="calendar_id", field_path="calendar_fk_id"),),
        metrics=(MetricSpec(alias="count", field_path="id", op=AggregateOp.COUNT),),
        parent_key=ParentKeySpec(alias=PARENT_KEY_ALIAS, field_path="calendar_fk_id"),
    )


class _FakeInfo:
    """Just enough of ``strawberry.Info`` for :func:`level_key`."""

    def __init__(self, path: Path) -> None:
        self.path = path


def _path(*keys) -> Path:
    path = None
    for key in keys:
        path = Path(path, key, None)
    assert path is not None
    return path


class TestLevelKey:
    def test_list_indices_are_dropped_so_every_parent_shares_one_level(self):
        third = _FakeInfo(_path("calendars", 3, "eventAggregate"))
        seventeenth = _FakeInfo(_path("calendars", 17, "eventAggregate"))

        assert level_key(third) == ("calendars", "eventAggregate")
        assert level_key(third) == level_key(seventeenth)

    def test_two_aliases_of_the_same_field_are_two_levels(self):
        """An alias is its own response key, so it gets its own batch.

        Without this, two selections of ``eventAggregate`` carrying different
        filters would share one batched result and the second would silently
        answer with the first one's rows.
        """
        booked = _FakeInfo(_path("calendars", 0, "booked"))
        cancelled = _FakeInfo(_path("calendars", 0, "cancelled"))

        assert level_key(booked) != level_key(cancelled)

    def test_a_root_level_field_is_a_one_element_path(self):
        assert level_key(_FakeInfo(_path("calendarEventAggregate"))) == ("calendarEventAggregate",)

    def test_nesting_under_two_lists_keeps_both_field_names(self):
        info = _FakeInfo(_path("calendarPools", 2, "calendars", 5, "eventAggregate"))

        assert level_key(info) == ("calendarPools", "calendars", "eventAggregate")


class TestBatchKey:
    def test_the_same_level_and_plan_is_one_batch(self):
        level = ("calendars", "eventAggregate")

        assert batch_key(level, _plan()) == batch_key(level, _plan())

    def test_the_same_plan_at_two_levels_is_two_batches(self):
        plan = _plan()

        assert batch_key(("calendars", "eventAggregate"), plan) != batch_key(
            ("appointmentTypes", "eventAggregate"), plan
        )

    def test_two_plans_at_one_level_are_two_batches(self):
        level = ("calendars", "eventAggregate")
        other = _plan((DimensionSpec(alias="timezone", field_path="timezone"),))

        assert batch_key(level, _plan()) != batch_key(level, other)


class TestGroupRowsByParent:
    def test_rows_are_bucketed_by_parent_and_keep_their_order(self):
        rows = [
            {PARENT_KEY_ALIAS: 1, "calendar_id": 1, "count": 3},
            {PARENT_KEY_ALIAS: 2, "calendar_id": 2, "count": 7},
            {PARENT_KEY_ALIAS: 1, "calendar_id": 1, "count": 5},
        ]

        buckets = group_rows_by_parent(rows, PARENT_KEY_ALIAS)

        assert set(buckets) == {1, 2}
        assert [row["count"] for row in buckets[1]] == [3, 5]
        assert [row["count"] for row in buckets[2]] == [7]

    def test_a_row_with_no_parent_belongs_to_nobody_and_is_dropped(self):
        """The pool link reaches its parent through a join, so an event on a
        calendar in no pool arrives with a null key. It is nobody's row."""
        rows = [
            {PARENT_KEY_ALIAS: None, "count": 9},
            {PARENT_KEY_ALIAS: 4, "count": 1},
        ]

        assert group_rows_by_parent(rows, PARENT_KEY_ALIAS) == {
            4: [{PARENT_KEY_ALIAS: 4, "count": 1}]
        }


class TestCollector:
    def test_twenty_five_parents_on_one_level_run_one_query(self):
        collector = NestedAggregateCollector()
        key = batch_key(("calendars", "eventAggregate"), _plan())
        runs = []

        def execute():
            runs.append(1)
            return {parent_id: [{"count": parent_id}] for parent_id in range(25)}

        results = [collector.rows_for(key, parent_id, execute) for parent_id in range(25)]

        assert len(runs) == 1
        assert collector.batch_count == 1
        assert [rows[0]["count"] for rows in results] == list(range(25))

    def test_a_parent_the_batch_found_nothing_for_does_not_run_a_second_query(self):
        collector = NestedAggregateCollector()
        key = batch_key(("calendars", "eventAggregate"), _plan())
        runs = []

        def execute():
            runs.append(1)
            return {1: [{"count": 2}]}

        assert collector.rows_for(key, 1, execute) == [{"count": 2}]
        assert collector.rows_for(key, 99, execute) == []
        assert len(runs) == 1

    def test_two_levels_are_two_queries(self):
        collector = NestedAggregateCollector()
        runs = []

        def execute():
            runs.append(1)
            return {}

        collector.rows_for(batch_key(("calendars", "eventAggregate"), _plan()), 1, execute)
        collector.rows_for(batch_key(("appointmentTypes", "eventAggregate"), _plan()), 1, execute)

        assert len(runs) == 2
        assert collector.batch_count == 2


class TestCollectorForRequest:
    def test_one_collector_per_request_object(self):
        class _Request:
            pass

        request = _Request()

        assert collector_for(request) is collector_for(request)
        assert collector_for(request) is not collector_for(_Request())


class TestLinks:
    """The four links the schema mounts, pinned against the registry.

    The paths are strings the ORM resolves at query time, so a model rename
    would otherwise surface as a ``FieldError`` from inside a resolver rather
    than as a failing test.
    """

    @pytest.mark.parametrize(
        ("link", "entity", "field_path"),
        [
            (CALENDAR_EVENTS, AggregatableEntity.CALENDAR_EVENT, "calendar_fk_id"),
            (CALENDAR_BLOCKED_TIMES, AggregatableEntity.BLOCKED_TIME, "calendar_fk_id"),
            (
                APPOINTMENT_TYPE_EVENTS,
                AggregatableEntity.CALENDAR_EVENT,
                "appointment_type_fk_id",
            ),
            (
                CALENDAR_POOL_EVENTS,
                AggregatableEntity.CALENDAR_EVENT,
                "calendar__pool_memberships__pool_fk_id",
            ),
        ],
    )
    def test_each_link_names_its_entity_and_parent_path(self, link, entity, field_path):
        assert link.entity is entity
        assert link.parent_field_path == field_path

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        "link",
        [CALENDAR_EVENTS, CALENDAR_BLOCKED_TIMES, APPOINTMENT_TYPE_EVENTS, CALENDAR_POOL_EVENTS],
    )
    def test_every_parent_path_resolves_against_its_model(self, link):
        from public_api.aggregations.registry import get_registration

        model = get_registration(link.entity).model
        # ``values()`` resolves the path eagerly; an unknown one raises
        # ``FieldError`` here rather than at the first partner query.
        str(model.objects.filter_by_organization(1).values(link.parent_field_path).query)
