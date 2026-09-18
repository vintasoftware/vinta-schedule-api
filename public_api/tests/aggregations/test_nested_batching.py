"""Nested aggregates end to end: one query per level, and the same numbers.

The failure this module exists to catch is a silent N+1 -- every value correct,
every functional assertion green, and the only symptom database load nobody
sees until production. So the load-bearing assertion here is not a value: it is
that the *same document* over five parents and over twenty-five parents issues
the identical number of queries. Without batching that reads 5 against 25.

The second half is the property that makes batching safe to do invisibly: a
nested aggregate returns exactly what the equivalent root aggregate grouped by
the same key returns for that parent. A batch that got the numbers wrong would
still pass the query-count test, and a batch that ran per parent would still
pass the numbers test, so neither stands on its own.
"""

import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from calendar_integration.models import CalendarPool, CalendarPoolMembership
from public_api.constants import PublicAPIResources
from public_api.tests.aggregations.conftest import (
    WINDOW_END,
    WINDOW_START,
    make_blocked_time,
    make_calendar,
    make_event,
    org_wide_token,
    post_graphql,
)


NESTED_QUERY = """
query Nested($start: DateTime!, $end: DateTime!) {
  calendars(limit: 100) {
    id
    eventAggregate(
      filter: {startDatetime: $start, endDatetime: $end}
      groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
      timezone: "UTC"
    ) {
      key { startTime }
      count
    }
  }
}
"""

ROOT_QUERY = """
query Root($start: DateTime!, $end: DateTime!) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{scalar: CALENDAR_ID}, {temporal: {field: START_TIME, granularity: DAY}}]
    timezone: "UTC"
    orderBy: [{key: CALENDAR_ID, direction: ASC}, {key: START_TIME, direction: ASC}]
  ) {
    key { calendarId startTime }
    count
  }
}
"""

TWO_AGGREGATES_QUERY = """
query Nested($start: DateTime!, $end: DateTime!) {
  calendars(limit: 100) {
    id
    eventAggregate(
      filter: {startDatetime: $start, endDatetime: $end}
      groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
      timezone: "UTC"
    ) { count }
    blockedTimeAggregate(
      filter: {startDatetime: $start, endDatetime: $end}
      groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
      timezone: "UTC"
    ) { count }
  }
}
"""

FRAGMENT_QUERY = """
fragment Rollup on CalendarGraphQLType {
  eventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
    timezone: "UTC"
  ) { count }
}

query Nested($start: DateTime!, $end: DateTime!) {
  calendars(limit: 100) { id ...Rollup }
}
"""

WITH_OWNERS_QUERY = """
query Nested($start: DateTime!, $end: DateTime!) {
  calendars(limit: 100) {
    id
    owners { membership { userId } }
    eventAggregate(
      filter: {startDatetime: $start, endDatetime: $end}
      groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
      timezone: "UTC"
    ) { count }
  }
}
"""

LIMITED_QUERY = """
query Nested($start: DateTime!, $end: DateTime!, $limit: Int!) {
  calendars(limit: 100) {
    id
    eventAggregate(
      filter: {startDatetime: $start, endDatetime: $end}
      groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
      timezone: "UTC"
      limit: $limit
    ) {
      key { startTime }
      count
    }
  }
}
"""

POOL_QUERY = """
query Pools($start: DateTime!, $end: DateTime!) {
  calendarPools(limit: 100) {
    id
    eventAggregate(
      filter: {startDatetime: $start, endDatetime: $end}
      groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
      timezone: "UTC"
    ) {
      key { startTime }
      count
    }
  }
}
"""


def _window() -> dict[str, str]:
    return {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()}


def _grouped_sql(captured) -> list[str]:
    return [query["sql"] for query in captured.captured_queries if "GROUP BY" in query["sql"]]


def _calendars_with_events(organization, count: int):
    """`count` calendars, the nth carrying n events spread over n days.

    Distinct per-calendar shapes, so a batch that leaked one calendar's rows
    into another's answer produces visibly wrong numbers rather than a
    coincidence.
    """
    calendars = []
    for index in range(1, count + 1):
        calendar = make_calendar(organization, name=f"cal-{index:03d}")
        for day in range(1, index + 1):
            make_event(
                organization,
                calendar,
                title=f"c{index}-d{day}",
                start=datetime.datetime(2026, 3, day, 9, 0),
                minutes=30,
            )
        calendars.append(calendar)
    return calendars


def _payload(response):
    body = response.json()
    assert body.get("errors", []) == [], body["errors"]
    return body["data"]


@pytest.mark.django_db
class TestQueryCountDoesNotGrowWithTheParentCount:
    def test_five_calendars_and_twenty_five_cost_the_same(self, organization):
        """The phase's acceptance criterion. Unbatched this reads 5 against 25."""
        system_user, token, auth = org_wide_token(
            organization, [PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT]
        )
        variables = _window()

        _calendars_with_events(organization, 5)
        with CaptureQueriesContext(connection) as five:
            five_response = post_graphql(NESTED_QUERY, system_user, token, auth, variables)

        _calendars_with_events(organization, 20)
        with CaptureQueriesContext(connection) as twenty_five:
            twenty_five_response = post_graphql(NESTED_QUERY, system_user, token, auth, variables)

        assert len(_payload(five_response)["calendars"]) == 5
        assert len(_payload(twenty_five_response)["calendars"]) == 25

        assert len(five.captured_queries) == len(twenty_five.captured_queries), (
            f"{len(five.captured_queries)} queries for five calendars, "
            f"{len(twenty_five.captured_queries)} for twenty-five -- the aggregate is "
            f"resolving per parent"
        )
        # Exactly one grouped query answers for every calendar at this level.
        assert len(_grouped_sql(twenty_five)) == 1, _grouped_sql(twenty_five)

    def test_the_one_grouped_query_carries_the_parent_column(self, organization):
        """Batching is a GROUP BY on the parent key, not a per-parent filter."""
        system_user, token, auth = org_wide_token(
            organization, [PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT]
        )
        _calendars_with_events(organization, 3)

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(NESTED_QUERY, system_user, token, auth, _window())

        _payload(response)
        grouped = _grouped_sql(captured)
        assert len(grouped) == 1, grouped
        assert "calendar_fk_id" in grouped[0]
        assert " IN (" in grouped[0]

    def test_two_sibling_aggregates_are_two_queries_not_two_per_parent(self, organization):
        """One query per distinct shape at a level -- and per shape only."""
        system_user, token, auth = org_wide_token(
            organization,
            [
                PublicAPIResources.CALENDAR,
                PublicAPIResources.CALENDAR_EVENT,
                PublicAPIResources.BLOCKED_TIME,
            ],
        )
        calendars = _calendars_with_events(organization, 6)
        for calendar in calendars:
            make_blocked_time(
                organization,
                calendar,
                reason="lunch",
                start=datetime.datetime(2026, 3, 2, 12, 0),
                minutes=60,
            )

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(TWO_AGGREGATES_QUERY, system_user, token, auth, _window())

        rows = _payload(response)["calendars"]
        assert len(rows) == 6
        assert all(row["blockedTimeAggregate"][0]["count"] == 1 for row in rows)
        assert len(_grouped_sql(captured)) == 2, _grouped_sql(captured)

    def test_an_aggregate_behind_a_named_fragment_still_batches(self, organization):
        """A fragment is how half of real documents spell a shared selection."""
        system_user, token, auth = org_wide_token(
            organization, [PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT]
        )
        _calendars_with_events(organization, 8)

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(FRAGMENT_QUERY, system_user, token, auth, _window())

        assert len(_payload(response)["calendars"]) == 8
        assert len(_grouped_sql(captured)) == 1, _grouped_sql(captured)

    def test_the_collector_does_not_defeat_the_optimizer(self, organization):
        """`owners` stays prefetched while the aggregate batches beside it.

        The collector reads the queryset `DjangoOptimizerExtension` already
        fetched. Recording it ahead of the optimizer would evaluate it first and
        leave the prefetch with nothing to attach to, which shows up here as a
        query count that grows with the calendar count.
        """
        system_user, token, auth = org_wide_token(
            organization, [PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT]
        )

        _calendars_with_events(organization, 4)
        with CaptureQueriesContext(connection) as few:
            few_response = post_graphql(WITH_OWNERS_QUERY, system_user, token, auth, _window())

        _calendars_with_events(organization, 16)
        with CaptureQueriesContext(connection) as many:
            many_response = post_graphql(WITH_OWNERS_QUERY, system_user, token, auth, _window())

        assert len(_payload(few_response)["calendars"]) == 4
        assert len(_payload(many_response)["calendars"]) == 20
        assert len(few.captured_queries) == len(many.captured_queries)


@pytest.mark.django_db
class TestTheBatchedPathAgreesWithTheDirectPath:
    def test_nested_and_root_aggregates_return_identical_numbers(self, organization):
        """The regression that makes batching invisible rather than merely fast."""
        system_user, token, auth = org_wide_token(
            organization, [PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT]
        )
        _calendars_with_events(organization, 5)
        variables = _window()

        nested = _payload(post_graphql(NESTED_QUERY, system_user, token, auth, variables))
        root = _payload(post_graphql(ROOT_QUERY, system_user, token, auth, variables))

        from_nested = {
            (calendar["id"], row["key"]["startTime"]): row["count"]
            for calendar in nested["calendars"]
            for row in calendar["eventAggregate"]
        }
        from_root = {
            (str(row["key"]["calendarId"]), row["key"]["startTime"]): row["count"]
            for row in root["calendarEventAggregate"]
        }

        assert from_nested == from_root
        # And the fixture really did produce something to disagree about:
        # 1 + 2 + 3 + 4 + 5 events across five calendars.
        assert sum(from_nested.values()) == 15
        assert len(from_nested) == 15

    def test_each_calendar_gets_only_its_own_rows(self, organization):
        """Per-calendar counts, hand-checked: the nth calendar has n days of one event."""
        system_user, token, auth = org_wide_token(
            organization, [PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT]
        )
        calendars = _calendars_with_events(organization, 4)

        response = post_graphql(NESTED_QUERY, system_user, token, auth, _window())

        by_id = {row["id"]: row["eventAggregate"] for row in _payload(response)["calendars"]}
        for index, calendar in enumerate(calendars, start=1):
            rows = by_id[str(calendar.id)]
            assert [row["count"] for row in rows] == [1] * index

    def test_a_parent_with_no_matching_rows_gets_an_empty_list(self, organization):
        """Sparse buckets, the same answer the root aggregate gives: no groups."""
        system_user, token, auth = org_wide_token(
            organization, [PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT]
        )
        _calendars_with_events(organization, 2)
        empty = make_calendar(organization, name="zzz-empty")

        response = post_graphql(NESTED_QUERY, system_user, token, auth, _window())

        by_id = {row["id"]: row["eventAggregate"] for row in _payload(response)["calendars"]}
        assert by_id[str(empty.id)] == []

    def test_the_limit_pages_each_parent_rather_than_the_whole_result(self, organization):
        """A shared slice would starve every parent after the first.

        Five calendars with five day-buckets each and `limit: 2`: every calendar
        gets its own first two days. Sliced globally, calendar one would take
        the page and the other four would come back empty -- which reads as "no
        events" rather than as a paging artefact.
        """
        system_user, token, auth = org_wide_token(
            organization, [PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT]
        )
        calendars = [make_calendar(organization, name=f"page-{index}") for index in range(5)]
        for calendar in calendars:
            for day in range(1, 6):
                make_event(
                    organization,
                    calendar,
                    title=f"{calendar.name}-d{day}",
                    start=datetime.datetime(2026, 3, day, 9, 0),
                    minutes=30,
                )

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(
                LIMITED_QUERY, system_user, token, auth, _window() | {"limit": 2}
            )

        rows = _payload(response)["calendars"]
        assert len(rows) == 5
        for row in rows:
            days = [entry["key"]["startTime"] for entry in row["eventAggregate"]]
            assert len(days) == 2, row
            assert days == sorted(days)
            assert days[0].startswith("2026-03-01")
            assert days[1].startswith("2026-03-02")

        # Still one query: the per-parent page is a ROW_NUMBER filter, which
        # Django renders as a wrapping subquery rather than a second statement.
        grouped = _grouped_sql(captured)
        assert len(grouped) == 1, grouped
        assert "ROW_NUMBER() OVER (PARTITION BY" in grouped[0]


@pytest.mark.django_db
class TestNestingUnderOtherParents:
    def test_a_pool_aggregates_the_events_of_its_roster(self, organization):
        """Reached through the pool's membership rows, still one query."""
        system_user, token, auth = org_wide_token(
            organization, [PublicAPIResources.CALENDAR_POOL, PublicAPIResources.CALENDAR_EVENT]
        )
        calendars = _calendars_with_events(organization, 3)
        pools = []
        for index, calendar in enumerate(calendars, start=1):
            pool = CalendarPool.objects.create(organization=organization, name=f"pool-{index}")
            CalendarPoolMembership.objects.create(
                organization=organization, pool=pool, calendar=calendar
            )
            pools.append(pool)

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(POOL_QUERY, system_user, token, auth, _window())

        by_id = {row["id"]: row["eventAggregate"] for row in _payload(response)["calendarPools"]}
        for index, pool in enumerate(pools, start=1):
            # The nth calendar carries n events on n distinct days, and its pool
            # rosters exactly that calendar.
            assert [row["count"] for row in by_id[str(pool.id)]] == [1] * index
        assert len(_grouped_sql(captured)) == 1, _grouped_sql(captured)

    def test_a_single_parent_still_runs_one_batched_query(self, organization):
        """A non-list parent records nothing; the batch is then a batch of one."""
        system_user, token, auth = org_wide_token(
            organization, [PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT]
        )
        _calendars_with_events(organization, 1)

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(NESTED_QUERY, system_user, token, auth, _window())

        assert len(_payload(response)["calendars"]) == 1
        assert len(_grouped_sql(captured)) == 1, _grouped_sql(captured)
