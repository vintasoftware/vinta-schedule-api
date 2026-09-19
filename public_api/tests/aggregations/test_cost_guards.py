"""The four things that bound what one aggregate can cost.

A grouped scan over a large tenant's history is the most expensive thing this
API can do, and no single guard covers it: a date range does not bound group
cardinality, a limit does not bound scan cost, and a timeout only turns a slow
query into a failed one.
"""

import datetime

from django.db import DatabaseError, connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

import pytest

from calendar_integration.models import CalendarEvent
from public_api.aggregations.fields import (
    _is_query_cancelled,
    _timeout_milliseconds,
    statement_timeout,
)
from public_api.constants import (
    AGGREGATE_STATEMENT_TIMEOUT_MS,
    LIMIT_OUT_OF_RANGE_MESSAGE,
    MAX_PAGE_SIZE,
    OFFSET_NEGATIVE_MESSAGE,
    PublicAPIResources,
)
from public_api.tests.aggregations.conftest import (
    assert_ok,
    event_window_variables,
    graphql_errors,
    make_calendar,
    make_event,
    org_wide_token,
    post_graphql,
)


PAGED_EVENTS = """
query Paged($filter: CalendarEventAggregateFilterInput!, $limit: Int!, $offset: Int!) {
    calendarEventAggregate(
        filter: $filter
        groupBy: [{scalar: CALENDAR_ID}]
        timezone: "UTC"
        limit: $limit
        offset: $offset
    ) {
        count
    }
}
"""

UNBOUNDED_EVENTS = """
query Unbounded {
    calendarEventAggregate(
        filter: {}
        groupBy: [{scalar: CALENDAR_ID}]
        timezone: "UTC"
    ) {
        count
    }
}
"""

BAD_TIMEZONE = """
query BadTimezone($filter: CalendarEventAggregateFilterInput!, $tz: String!) {
    calendarEventAggregate(
        filter: $filter
        groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
        timezone: $tz
    ) {
        count
    }
}
"""


@pytest.fixture
def credentials(organization):
    return org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])


@pytest.fixture
def one_event(organization):
    calendar = make_calendar(organization, "A")
    make_event(organization, calendar, title="Alpha", day=2, minutes=30)
    return calendar


@pytest.mark.django_db
class TestLimitAndOffset:
    @pytest.mark.parametrize("limit", [0, -1, MAX_PAGE_SIZE + 1, 1000])
    def test_a_limit_outside_the_range_is_refused(
        self, api_client, organization, credentials, one_event, limit
    ):
        response = post_graphql(
            api_client,
            PAGED_EVENTS,
            credentials,
            {**event_window_variables(), "limit": limit, "offset": 0},
        )

        assert LIMIT_OUT_OF_RANGE_MESSAGE in graphql_errors(response)

    def test_the_message_is_the_one_the_list_fields_use(self):
        """One wording for the whole surface, so a partner learns it once."""
        assert LIMIT_OUT_OF_RANGE_MESSAGE == f"Limit must be between 1 and {MAX_PAGE_SIZE}"

    def test_a_negative_offset_is_refused(self, api_client, organization, credentials, one_event):
        response = post_graphql(
            api_client,
            PAGED_EVENTS,
            credentials,
            {**event_window_variables(), "limit": 10, "offset": -1},
        )

        assert OFFSET_NEGATIVE_MESSAGE in graphql_errors(response)

    @pytest.mark.parametrize("limit", [1, MAX_PAGE_SIZE])
    def test_the_bounds_themselves_are_accepted(
        self, api_client, organization, credentials, one_event, limit
    ):
        response = post_graphql(
            api_client,
            PAGED_EVENTS,
            credentials,
            {**event_window_variables(), "limit": limit, "offset": 0},
        )

        assert assert_ok(response)["calendarEventAggregate"] == [{"count": 1}]


@pytest.mark.django_db
class TestBoundedRange:
    def test_a_query_without_bounds_fails_validation(
        self, api_client, organization, credentials, one_event
    ):
        """``startDatetime`` and ``endDatetime`` are non-null on the input type."""
        response = post_graphql(api_client, UNBOUNDED_EVENTS, credentials)

        errors = graphql_errors(response)
        assert errors
        assert any("startDatetime" in error for error in errors), errors
        assert response.json().get("data") is None

    def test_an_over_long_range_is_refused(self, api_client, organization, credentials, one_event):
        start = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        variables = {
            "filter": {
                "startDatetime": start.isoformat(),
                "endDatetime": (start + datetime.timedelta(days=400)).isoformat(),
            }
        }
        query = """
        query TooLong($filter: CalendarEventAggregateFilterInput!) {
            calendarEventAggregate(
                filter: $filter
                groupBy: [{scalar: CALENDAR_ID}]
                timezone: "UTC"
            ) { count }
        }
        """

        response = post_graphql(api_client, query, credentials, variables)

        assert "Requested time range is too large." in graphql_errors(response)

    def test_a_backwards_range_is_refused(self, api_client, organization, credentials, one_event):
        moment = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC).isoformat()
        query = """
        query Backwards($filter: CalendarEventAggregateFilterInput!) {
            calendarEventAggregate(
                filter: $filter
                groupBy: [{scalar: CALENDAR_ID}]
                timezone: "UTC"
            ) { count }
        }
        """

        response = post_graphql(
            api_client,
            query,
            credentials,
            {"filter": {"startDatetime": moment, "endDatetime": moment}},
        )

        assert "Invalid time range: endDatetime must be after startDatetime." in graphql_errors(
            response
        )


@pytest.mark.django_db
class TestGroupByGuards:
    def test_an_empty_group_by_is_refused(self, api_client, organization, credentials, one_event):
        """An aggregate with no dimension is one unbounded row over everything."""
        query = """
        query NoDimensions($filter: CalendarEventAggregateFilterInput!) {
            calendarEventAggregate(
                filter: $filter
                groupBy: []
                timezone: "UTC"
            ) { count }
        }
        """

        response = post_graphql(api_client, query, credentials, event_window_variables())

        assert "An aggregate query must group by at least one dimension" in graphql_errors(response)

    def test_an_unknown_timezone_is_refused_without_echoing_it(
        self, api_client, organization, credentials, one_event
    ):
        response = post_graphql(
            api_client,
            BAD_TIMEZONE,
            credentials,
            {**event_window_variables(), "tz": "Mars/Olympus_Mons"},
        )

        errors = graphql_errors(response)
        assert "Unknown timezone" in errors
        assert not any("Mars" in error for error in errors)

    def test_a_granularity_on_a_scalar_dimension_cannot_be_written(
        self, api_client, organization, credentials, one_event
    ):
        """The schema refuses it, so no resolver ever has to."""
        query = """
        query BadGranularity($filter: CalendarEventAggregateFilterInput!) {
            calendarEventAggregate(
                filter: $filter
                groupBy: [{scalar: CALENDAR_ID, granularity: DAY}]
                timezone: "UTC"
            ) { count }
        }
        """

        response = post_graphql(api_client, query, credentials, event_window_variables())

        errors = graphql_errors(response)
        assert any("granularity" in error for error in errors), errors


@pytest.mark.django_db
class TestStatementTimeout:
    def test_the_timeout_is_set_for_the_aggregate_and_put_back_after(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('statement_timeout')")
            before = cursor.fetchone()[0]

        with statement_timeout(1234):
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('statement_timeout')")
                during = cursor.fetchone()[0]

        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('statement_timeout')")
            after = cursor.fetchone()[0]

        assert during == "1234ms"
        assert after == before

    def test_a_statement_over_the_budget_is_cancelled(self):
        """A one-millisecond budget makes even a trivial sleep fail."""
        with pytest.raises(DatabaseError) as excinfo:
            with statement_timeout(1):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_sleep(1)")

        assert _is_query_cancelled(excinfo.value)

    def test_an_aggregate_over_the_budget_returns_the_documented_error(
        self, api_client, organization, credentials, one_event
    ):
        with override_settings(PUBLIC_API_AGGREGATE_STATEMENT_TIMEOUT_MS=1):
            response = post_graphql(
                api_client,
                PAGED_EVENTS,
                credentials,
                {**event_window_variables(), "limit": 10, "offset": 0},
            )

        errors = graphql_errors(response)
        # A trivial aggregate over one row can beat a 1ms budget, so accept
        # either outcome — what must never happen is a 500 or a raw driver
        # message reaching the caller.
        assert errors in ([], ["Aggregate query exceeded its time budget"]), errors
        assert response.status_code == 200

    def test_the_budget_comes_from_settings_with_a_constant_default(self):
        assert _timeout_milliseconds() == AGGREGATE_STATEMENT_TIMEOUT_MS

        with override_settings(PUBLIC_API_AGGREGATE_STATEMENT_TIMEOUT_MS=250):
            assert _timeout_milliseconds() == 250


@pytest.mark.django_db
class TestScanShape:
    def test_group_count_stays_bounded_by_the_limit(self, api_client, organization, credentials):
        """Twenty calendars, a limit of five: five rows, one statement."""
        for index in range(20):
            calendar = make_calendar(organization, f"Calendar {index}")
            make_event(organization, calendar, title=f"Event {index}", day=2, minutes=30)

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(
                api_client,
                PAGED_EVENTS,
                credentials,
                {**event_window_variables(), "limit": 5, "offset": 0},
            )

        assert len(assert_ok(response)["calendarEventAggregate"]) == 5
        grouped = [
            query["sql"]
            for query in captured.captured_queries
            if "GROUP BY" in query["sql"].upper() and CalendarEvent._meta.db_table in query["sql"]
        ]
        assert len(grouped) == 1
        assert "LIMIT 5" in grouped[0]
