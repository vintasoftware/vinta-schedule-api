"""Ordering grouped rows by a metric, and taking the top N.

"The three busiest calendars" is the question; the answer has to be the same
three in the same order every time it is asked, which is what the group-key
tiebreak is for. Two of the calendars here tie deliberately.
"""

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from calendar_integration.models import CalendarEvent
from public_api.constants import PublicAPIResources
from public_api.tests.aggregations.conftest import (
    assert_ok,
    event_window_variables,
    graphql_errors,
    make_calendar,
    make_event,
    org_wide_token,
    post_graphql,
)


ORDERED_EVENTS = """
query Ordered(
    $filter: CalendarEventAggregateFilterInput!
    $orderBy: [CalendarEventAggregateOrderInput!]
    $limit: Int!
) {
    calendarEventAggregate(
        filter: $filter
        groupBy: [{scalar: CALENDAR_ID}]
        timezone: "UTC"
        orderBy: $orderBy
        limit: $limit
    ) {
        key { calendarId }
        count
    }
}
"""

TOP_BUSIEST = """
query TopBusiest(
    $filter: CalendarEventAggregateFilterInput!
    $having: CalendarEventHavingInput
    $orderBy: [CalendarEventAggregateOrderInput!]
    $limit: Int!
) {
    calendarEventAggregate(
        filter: $filter
        groupBy: [{scalar: CALENDAR_ID}]
        timezone: "UTC"
        having: $having
        orderBy: $orderBy
        limit: $limit
    ) {
        key { calendarId }
        count
    }
}
"""

BY_DAY = """
query ByDay(
    $filter: CalendarEventAggregateFilterInput!
    $orderBy: [CalendarEventAggregateOrderInput!]
) {
    calendarEventAggregate(
        filter: $filter
        groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
        timezone: "UTC"
        orderBy: $orderBy
    ) {
        key { startTime }
        count
    }
}
"""


def _grouped_statement(captured: CaptureQueriesContext) -> str:
    grouped = [
        query["sql"]
        for query in captured.captured_queries
        if "GROUP BY" in query["sql"].upper() and CalendarEvent._meta.db_table in query["sql"]
    ]
    assert len(grouped) == 1, grouped
    return grouped[0]


@pytest.fixture
def credentials(organization):
    return org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])


@pytest.fixture
def calendars(organization):
    """Five calendars: 5, 4, 3, 3 and 1 events.

    The two threes tie on purpose — "the top three" has to pick between them the
    same way every call, and only the tiebreak makes that true.
    """
    made = {}
    for name, event_count in (
        ("busiest", 5),
        ("second", 4),
        ("tied_a", 3),
        ("tied_b", 3),
        ("quietest", 1),
    ):
        calendar = make_calendar(organization, f"Calendar {name}")
        for index in range(event_count):
            make_event(organization, calendar, title=f"{name}-{index}", day=2, minutes=30 + index)
        made[name] = calendar
    return made


@pytest.mark.django_db
class TestOrderingByAMetric:
    def test_the_three_busiest_come_back_in_order(
        self, api_client, organization, credentials, calendars
    ):
        response = post_graphql(
            api_client,
            ORDERED_EVENTS,
            credentials,
            {
                **event_window_variables(),
                "orderBy": [{"metric": "COUNT", "direction": "DESC"}],
                "limit": 3,
            },
        )

        rows = assert_ok(response)["calendarEventAggregate"]
        assert [row["count"] for row in rows] == [5, 4, 3]
        assert rows[0]["key"]["calendarId"] == calendars["busiest"].id
        assert rows[1]["key"]["calendarId"] == calendars["second"].id

    def test_ascending_reverses_it(self, api_client, organization, credentials, calendars):
        response = post_graphql(
            api_client,
            ORDERED_EVENTS,
            credentials,
            {
                **event_window_variables(),
                "orderBy": [{"metric": "COUNT", "direction": "ASC"}],
                "limit": 3,
            },
        )

        rows = assert_ok(response)["calendarEventAggregate"]
        assert [row["count"] for row in rows] == [1, 3, 3]

    def test_ties_break_the_same_way_on_every_run(
        self, api_client, organization, credentials, calendars
    ):
        """Without the group-key tiebreak this is a coin toss per call."""
        variables = {
            **event_window_variables(),
            "orderBy": [{"metric": "COUNT", "direction": "DESC"}],
            "limit": 4,
        }

        runs = [
            [
                row["key"]["calendarId"]
                for row in assert_ok(
                    post_graphql(api_client, ORDERED_EVENTS, credentials, variables)
                )["calendarEventAggregate"]
            ]
            for _ in range(3)
        ]

        assert runs[0] == runs[1] == runs[2]
        # The tie is broken by the group key, ascending.
        assert runs[0][2:] == sorted([calendars["tied_a"].id, calendars["tied_b"].id])

    def test_the_group_key_is_appended_to_the_sql_ordering(
        self, api_client, organization, credentials, calendars
    ):
        with CaptureQueriesContext(connection) as captured:
            post_graphql(
                api_client,
                ORDERED_EVENTS,
                credentials,
                {
                    **event_window_variables(),
                    "orderBy": [{"metric": "COUNT", "direction": "DESC"}],
                    "limit": 5,
                },
            )

        order_clause = _grouped_statement(captured).upper().split("ORDER BY")[1]
        # Two terms: the metric, then the key.
        assert order_clause.count(",") == 1
        assert "DESC" in order_clause

    def test_paging_the_ordered_result_sees_each_group_once(
        self, api_client, organization, credentials, calendars
    ):
        query = ORDERED_EVENTS.replace("$limit: Int!", "$limit: Int!, $offset: Int!").replace(
            "limit: $limit", "limit: $limit\n        offset: $offset"
        )
        base = {
            **event_window_variables(),
            "orderBy": [{"metric": "COUNT", "direction": "DESC"}],
            "limit": 2,
        }

        pages = [
            assert_ok(post_graphql(api_client, query, credentials, {**base, "offset": offset}))[
                "calendarEventAggregate"
            ]
            for offset in (0, 2, 4)
        ]

        seen = [row["key"]["calendarId"] for page in pages for row in page]
        assert len(seen) == 5
        assert len(set(seen)) == 5

    def test_ordering_by_a_metric_the_document_does_not_display(
        self, api_client, organization, credentials, calendars
    ):
        """The engine annotates it to sort on it, and never reads it back."""
        query = """
        query SortOnly(
            $filter: CalendarEventAggregateFilterInput!
            $orderBy: [CalendarEventAggregateOrderInput!]
        ) {
            calendarEventAggregate(
                filter: $filter
                groupBy: [{scalar: CALENDAR_ID}]
                timezone: "UTC"
                orderBy: $orderBy
            ) {
                key { calendarId }
            }
        }
        """

        response = post_graphql(
            api_client,
            query,
            credentials,
            {
                **event_window_variables(),
                "orderBy": [{"metric": "DURATION_MINUTES_SUM", "direction": "DESC"}],
            },
        )

        rows = assert_ok(response)["calendarEventAggregate"]
        # The busiest calendar has five events and the longest total.
        assert rows[0]["key"]["calendarId"] == calendars["busiest"].id
        assert "durationMinutes" not in rows[0]


@pytest.mark.django_db
class TestOrderingByAKey:
    def test_a_scalar_key_orders_on_the_dimension(
        self, api_client, organization, credentials, calendars
    ):
        response = post_graphql(
            api_client,
            ORDERED_EVENTS,
            credentials,
            {
                **event_window_variables(),
                "orderBy": [{"key": "CALENDAR_ID", "direction": "DESC"}],
                "limit": 5,
            },
        )

        ids = [row["key"]["calendarId"] for row in assert_ok(response)["calendarEventAggregate"]]
        assert ids == sorted(ids, reverse=True)

    def test_a_bucketed_key_orders_on_the_bucket(self, api_client, organization, credentials):
        calendar = make_calendar(organization, "Spread")
        for day in (2, 3, 4):
            make_event(organization, calendar, title=f"day-{day}", day=day, minutes=30)

        response = post_graphql(
            api_client,
            BY_DAY,
            credentials,
            {**event_window_variables(), "orderBy": [{"key": "START_TIME", "direction": "DESC"}]},
        )

        buckets = [row["key"]["startTime"] for row in assert_ok(response)["calendarEventAggregate"]]
        assert buckets == sorted(buckets, reverse=True)

    def test_ordering_by_a_dimension_the_query_does_not_group_by_is_refused(
        self, api_client, organization, credentials, calendars
    ):
        response = post_graphql(
            api_client,
            ORDERED_EVENTS,
            credentials,
            {
                **event_window_variables(),
                "orderBy": [{"key": "TIMEZONE"}],
                "limit": 5,
            },
        )

        assert (
            "Cannot order by a dimension the query does not group by: timezone"
            in graphql_errors(response)
        )

    def test_setting_both_key_and_metric_is_refused(
        self, api_client, organization, credentials, calendars
    ):
        response = post_graphql(
            api_client,
            ORDERED_EVENTS,
            credentials,
            {
                **event_window_variables(),
                "orderBy": [{"key": "CALENDAR_ID", "metric": "COUNT"}],
                "limit": 5,
            },
        )

        assert "An order-by must set exactly one of key and metric" in graphql_errors(response)

    def test_setting_neither_key_nor_metric_is_refused(
        self, api_client, organization, credentials, calendars
    ):
        response = post_graphql(
            api_client,
            ORDERED_EVENTS,
            credentials,
            {**event_window_variables(), "orderBy": [{"direction": "ASC"}], "limit": 5},
        )

        assert "An order-by must set exactly one of key and metric" in graphql_errors(response)


@pytest.mark.django_db
class TestTopN:
    def test_the_acceptance_query(self, api_client, organization, credentials, calendars):
        """``having`` + ``orderBy`` + ``limit``, computed entirely in SQL."""
        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(
                api_client,
                TOP_BUSIEST,
                credentials,
                {
                    **event_window_variables(),
                    "having": {"count": {"gt": 2}},
                    "orderBy": [{"metric": "COUNT", "direction": "DESC"}],
                    "limit": 3,
                },
            )

        rows = assert_ok(response)["calendarEventAggregate"]
        assert [row["count"] for row in rows] == [5, 4, 3]
        assert calendars["quietest"].id not in {row["key"]["calendarId"] for row in rows}

        sql = _grouped_statement(captured).upper()
        assert "HAVING" in sql
        assert "ORDER BY" in sql
        assert "LIMIT 3" in sql

    def test_the_whole_thing_is_one_statement(
        self, api_client, organization, credentials, calendars
    ):
        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(
                api_client,
                TOP_BUSIEST,
                credentials,
                {
                    **event_window_variables(),
                    "having": {"count": {"gt": 2}},
                    "orderBy": [{"metric": "COUNT", "direction": "DESC"}],
                    "limit": 3,
                },
            )

        assert len(assert_ok(response)["calendarEventAggregate"]) == 3
        # ``_grouped_statement`` asserts there is exactly one.
        assert _grouped_statement(captured)

    def test_a_having_that_leaves_fewer_than_the_limit_returns_what_is_left(
        self, api_client, organization, credentials, calendars
    ):
        response = post_graphql(
            api_client,
            TOP_BUSIEST,
            credentials,
            {
                **event_window_variables(),
                "having": {"count": {"gte": 4}},
                "orderBy": [{"metric": "COUNT", "direction": "DESC"}],
                "limit": 10,
            },
        )

        assert [row["count"] for row in assert_ok(response)["calendarEventAggregate"]] == [5, 4]
