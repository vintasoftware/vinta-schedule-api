"""HAVING against real rows, end to end and in one statement.

The thing being proved is that the filtering happens in Postgres. A Python
post-filter would return the same rows and pass a naive assertion, so every test
here either reads the emitted SQL or counts the statements.
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
    make_blocked_time,
    make_calendar,
    make_event,
    org_wide_token,
    post_graphql,
)


COUNT_HAVING = """
query CountHaving($filter: CalendarEventAggregateFilterInput!, $having: CalendarEventHavingInput) {
    calendarEventAggregate(
        filter: $filter
        groupBy: [{scalar: CALENDAR_ID}]
        timezone: "UTC"
        having: $having
    ) {
        key { calendarId }
        count
    }
}
"""

DURATION_HAVING = """
query DurationHaving(
    $filter: CalendarEventAggregateFilterInput!
    $having: CalendarEventHavingInput
) {
    calendarEventAggregate(
        filter: $filter
        groupBy: [{scalar: CALENDAR_ID}]
        timezone: "UTC"
        having: $having
    ) {
        key { calendarId }
        count
        durationMinutes { sum }
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
def ragged_calendars(organization):
    """Three calendars holding one, two and three events.

    ``count > 2`` keeps exactly the third, which is what makes the test able to
    tell a real ``HAVING`` from a no-op.
    """
    calendars = {}
    for name, event_count in (("one", 1), ("two", 2), ("three", 3)):
        calendar = make_calendar(organization, f"Calendar {name}")
        for index in range(event_count):
            make_event(
                organization,
                calendar,
                title=f"{name}-{index}",
                day=2 + index,
                minutes=30 * (index + 1),
            )
        calendars[name] = calendar
    return calendars


@pytest.mark.django_db
class TestCountHaving:
    def test_it_keeps_only_the_groups_over_the_threshold(
        self, api_client, organization, credentials, ragged_calendars
    ):
        response = post_graphql(
            api_client,
            COUNT_HAVING,
            credentials,
            {**event_window_variables(), "having": {"count": {"gt": 2}}},
        )

        assert assert_ok(response)["calendarEventAggregate"] == [
            {"key": {"calendarId": ragged_calendars["three"].id}, "count": 3}
        ]

    def test_the_filtering_happens_in_postgres(
        self, api_client, organization, credentials, ragged_calendars
    ):
        """A ``HAVING`` in the statement, and one statement."""
        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(
                api_client,
                COUNT_HAVING,
                credentials,
                {**event_window_variables(), "having": {"count": {"gt": 2}}},
            )

        assert len(assert_ok(response)["calendarEventAggregate"]) == 1
        assert "HAVING" in _grouped_statement(captured).upper()

    def test_without_a_having_clause_every_group_comes_back(
        self, api_client, organization, credentials, ragged_calendars
    ):
        response = post_graphql(api_client, COUNT_HAVING, credentials, event_window_variables())

        rows = assert_ok(response)["calendarEventAggregate"]
        assert sorted(row["count"] for row in rows) == [1, 2, 3]

    def test_an_empty_having_clause_behaves_like_no_clause(
        self, api_client, organization, credentials, ragged_calendars
    ):
        """``having: {}`` is a legal document meaning "no constraint"."""
        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(
                api_client,
                COUNT_HAVING,
                credentials,
                {**event_window_variables(), "having": {}},
            )

        rows = assert_ok(response)["calendarEventAggregate"]
        assert sorted(row["count"] for row in rows) == [1, 2, 3]
        assert "HAVING" not in _grouped_statement(captured).upper()

    @pytest.mark.parametrize(
        ("comparison", "expected"),
        [
            ({"gt": 2}, [3]),
            ({"gte": 2}, [2, 3]),
            ({"lt": 2}, [1]),
            ({"lte": 2}, [1, 2]),
            ({"eq": 2}, [2]),
        ],
    )
    def test_every_comparison_operator_reaches_the_database(
        self, api_client, organization, credentials, ragged_calendars, comparison, expected
    ):
        response = post_graphql(
            api_client,
            COUNT_HAVING,
            credentials,
            {**event_window_variables(), "having": {"count": comparison}},
        )

        rows = assert_ok(response)["calendarEventAggregate"]
        assert sorted(row["count"] for row in rows) == expected

    def test_two_comparisons_on_one_metric_are_anded(
        self, api_client, organization, credentials, ragged_calendars
    ):
        response = post_graphql(
            api_client,
            COUNT_HAVING,
            credentials,
            {**event_window_variables(), "having": {"count": {"gte": 2, "lte": 2}}},
        )

        rows = assert_ok(response)["calendarEventAggregate"]
        assert [row["count"] for row in rows] == [2]

    def test_a_threshold_nothing_meets_returns_an_empty_list(
        self, api_client, organization, credentials, ragged_calendars
    ):
        response = post_graphql(
            api_client,
            COUNT_HAVING,
            credentials,
            {**event_window_variables(), "having": {"count": {"gt": 100}}},
        )

        assert assert_ok(response)["calendarEventAggregate"] == []


@pytest.mark.django_db
class TestCompositeHaving:
    def test_or_keeps_a_group_matching_either_branch(
        self, api_client, organization, credentials, ragged_calendars
    ):
        response = post_graphql(
            api_client,
            COUNT_HAVING,
            credentials,
            {
                **event_window_variables(),
                "having": {"or": [{"count": {"lt": 2}}, {"count": {"gt": 2}}]},
            },
        )

        rows = assert_ok(response)["calendarEventAggregate"]
        assert sorted(row["count"] for row in rows) == [1, 3]

    def test_and_narrows_rather_than_widens(
        self, api_client, organization, credentials, ragged_calendars
    ):
        response = post_graphql(
            api_client,
            COUNT_HAVING,
            credentials,
            {
                **event_window_variables(),
                "having": {"count": {"gte": 2}, "and": [{"count": {"lte": 2}}]},
            },
        )

        rows = assert_ok(response)["calendarEventAggregate"]
        assert [row["count"] for row in rows] == [2]

    def test_a_top_level_condition_is_anded_with_an_or_group(
        self, api_client, organization, credentials, ragged_calendars
    ):
        """``count >= 2 AND (count < 2 OR count > 2)`` leaves only the three."""
        response = post_graphql(
            api_client,
            COUNT_HAVING,
            credentials,
            {
                **event_window_variables(),
                "having": {
                    "count": {"gte": 2},
                    "or": [{"count": {"lt": 2}}, {"count": {"gt": 2}}],
                },
            },
        )

        rows = assert_ok(response)["calendarEventAggregate"]
        assert [row["count"] for row in rows] == [3]

    def test_nesting_deeper_than_the_limit_is_refused(
        self, api_client, organization, credentials, ragged_calendars
    ):
        having: dict = {"count": {"gt": 0}}
        for _ in range(5):
            having = {"and": [having]}

        response = post_graphql(
            api_client,
            COUNT_HAVING,
            credentials,
            {**event_window_variables(), "having": having},
        )

        assert "A having clause may not nest more than 5 levels deep" in graphql_errors(response)


@pytest.mark.django_db
class TestHavingOnAnUnselectedMetric:
    def test_a_clause_may_name_a_metric_the_document_does_not_display(
        self, api_client, organization, credentials, ragged_calendars
    ):
        """The engine annotates it, filters on it, and never reads it back."""
        response = post_graphql(
            api_client,
            COUNT_HAVING,
            credentials,
            {
                **event_window_variables(),
                "having": {"durationMinutes": {"sum": {"gt": 100.0}}},
            },
        )

        rows = assert_ok(response)["calendarEventAggregate"]
        # "three" holds 30 + 60 + 90 = 180 minutes; "two" holds 30 + 60 = 90.
        assert [row["count"] for row in rows] == [3]
        assert "durationMinutes" not in rows[0]

    def test_the_unselected_metric_is_still_one_statement(
        self, api_client, organization, credentials, ragged_calendars
    ):
        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(
                api_client,
                COUNT_HAVING,
                credentials,
                {
                    **event_window_variables(),
                    "having": {"durationMinutes": {"sum": {"gt": 100.0}}},
                },
            )

        assert len(assert_ok(response)["calendarEventAggregate"]) == 1
        sql = _grouped_statement(captured).upper()
        assert "HAVING" in sql
        assert "SUM(" in sql

    def test_naming_a_metric_the_document_also_selects_computes_it_once(
        self, api_client, organization, credentials, ragged_calendars
    ):
        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(
                api_client,
                DURATION_HAVING,
                credentials,
                {
                    **event_window_variables(),
                    "having": {"durationMinutes": {"sum": {"gt": 100.0}}},
                },
            )

        rows = assert_ok(response)["calendarEventAggregate"]
        assert rows == [
            {
                "key": {"calendarId": ragged_calendars["three"].id},
                "count": 3,
                "durationMinutes": {"sum": 180.0},
            }
        ]
        # One selected column, not two: the aliases the selection set and the
        # having clause produce are the same. Postgres cannot reference a select
        # alias from HAVING, so Django repeats the expression there — that
        # repetition is the SQL standard, not a second aggregate.
        sql = _grouped_statement(captured)
        assert sql.count('AS "metric_duration_minutes_sum"') == 1


@pytest.mark.django_db
class TestHavingOnARelationCount:
    def test_it_filters_calendars_by_how_many_events_they_hold(self, api_client, organization):
        calendar_credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR])
        busy = make_calendar(organization, "Busy")
        quiet = make_calendar(organization, "Quiet")
        for index in range(3):
            make_event(organization, busy, title=f"busy-{index}", day=2, minutes=30)
        make_event(organization, quiet, title="quiet", day=2, minutes=30)
        make_blocked_time(organization, busy, day=2)

        query = """
        query BusyCalendars($filter: CalendarAggregateFilterInput!, $having: CalendarHavingInput) {
            calendarAggregate(
                filter: $filter
                groupBy: [{scalar: PROVIDER}]
                timezone: "UTC"
                having: $having
            ) {
                count
                eventsCount
            }
        }
        """

        matching = post_graphql(
            api_client,
            query,
            calendar_credentials,
            {"filter": {}, "having": {"eventsCount": {"gte": 4}}},
        )
        missing = post_graphql(
            api_client,
            query,
            calendar_credentials,
            {"filter": {}, "having": {"eventsCount": {"gt": 100}}},
        )

        # Both calendars share the one provider group, so the group holds four
        # events between them.
        assert assert_ok(matching)["calendarAggregate"] == [{"count": 2, "eventsCount": 4}]
        assert assert_ok(missing)["calendarAggregate"] == []
