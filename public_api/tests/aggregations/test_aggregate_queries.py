"""End-to-end through the schema, for all six entities.

These are the tests that prove the wiring: a document goes in over HTTP, one
grouped statement runs, and the numbers that come back are the ones Postgres
computed.
"""

import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from calendar_integration.models import CalendarEvent
from public_api.constants import PublicAPIResources
from public_api.tests.aggregations.conftest import (
    assert_ok,
    event_window_variables,
    make_appointment_type,
    make_available_time,
    make_blocked_time,
    make_calendar,
    make_calendar_pool,
    make_event,
    org_wide_token,
    post_graphql,
)


EVENTS_BY_CALENDAR = """
query EventAggregate($filter: CalendarEventAggregateFilterInput!) {
    calendarEventAggregate(
        filter: $filter
        groupBy: [{scalar: CALENDAR_ID}]
        timezone: "UTC"
    ) {
        key { calendarId }
        count
        durationMinutes { sum avg min max }
    }
}
"""

EVENTS_WITH_TITLES = """
query EventTitles($filter: CalendarEventAggregateFilterInput!) {
    calendarEventAggregate(
        filter: $filter
        groupBy: [{scalar: CALENDAR_ID}]
        timezone: "UTC"
    ) {
        key { calendarId }
        count
        title { concat(separator: "; ") min max }
    }
}
"""

EVENTS_BY_DAY = """
query EventsByDay($filter: CalendarEventAggregateFilterInput!, $tz: String!) {
    calendarEventAggregate(
        filter: $filter
        groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
        timezone: $tz
    ) {
        key { startTime }
        count
    }
}
"""


def _rows_by_calendar(rows: list[dict]) -> dict[int, dict]:
    return {row["key"]["calendarId"]: row for row in rows}


@pytest.fixture
def calendar_a(organization):
    return make_calendar(organization, "Calendar A")


@pytest.fixture
def calendar_b(organization):
    return make_calendar(organization, "Calendar B")


@pytest.fixture
def events(organization, calendar_a, calendar_b):
    """A: 30 + 60 + 90 minutes over three days. B: one 45-minute event."""
    make_event(organization, calendar_a, title="Alpha", day=2, minutes=30)
    make_event(organization, calendar_a, title="Bravo", day=3, minutes=60)
    make_event(organization, calendar_a, title="Charlie", day=4, minutes=90)
    make_event(organization, calendar_b, title="Delta", day=2, minutes=45)


@pytest.mark.django_db
class TestCalendarEventAggregate:
    def test_grouped_by_calendar_with_numeric_rollups(
        self, api_client, organization, calendar_a, calendar_b, events
    ):
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        response = post_graphql(
            api_client, EVENTS_BY_CALENDAR, credentials, event_window_variables()
        )

        rows = _rows_by_calendar(assert_ok(response)["calendarEventAggregate"])
        assert rows[calendar_a.id] == {
            "key": {"calendarId": calendar_a.id},
            "count": 3,
            "durationMinutes": {"sum": 180.0, "avg": 60.0, "min": 30.0, "max": 90.0},
        }
        assert rows[calendar_b.id] == {
            "key": {"calendarId": calendar_b.id},
            "count": 1,
            "durationMinutes": {"sum": 45.0, "avg": 45.0, "min": 45.0, "max": 45.0},
        }

    def test_string_concat_returns_the_joined_titles(
        self, api_client, organization, calendar_a, calendar_b, events
    ):
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        response = post_graphql(
            api_client, EVENTS_WITH_TITLES, credentials, event_window_variables()
        )

        rows = _rows_by_calendar(assert_ok(response)["calendarEventAggregate"])
        assert rows[calendar_a.id]["title"] == {
            "concat": "Alpha; Bravo; Charlie",
            "min": "Alpha",
            "max": "Charlie",
        }
        assert rows[calendar_b.id]["title"]["concat"] == "Delta"

    def test_an_unselected_aggregate_is_null_rather_than_zero(
        self, api_client, organization, calendar_a, calendar_b, events
    ):
        """Only what the document asked for is computed."""
        query = """
        query Sparse($filter: CalendarEventAggregateFilterInput!) {
            calendarEventAggregate(
                filter: $filter
                groupBy: [{scalar: CALENDAR_ID}]
                timezone: "UTC"
            ) {
                count
                durationMinutes { sum }
                title { min }
            }
        }
        """
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        response = post_graphql(api_client, query, credentials, event_window_variables())

        rows = assert_ok(response)["calendarEventAggregate"]
        assert all(row["durationMinutes"]["sum"] is not None for row in rows)
        assert all(row["title"]["min"] is not None for row in rows)

    def test_grouped_by_day_in_a_named_timezone(
        self, api_client, organization, calendar_a, calendar_b, events
    ):
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        response = post_graphql(
            api_client,
            EVENTS_BY_DAY,
            credentials,
            {**event_window_variables(), "tz": "America/Sao_Paulo"},
        )

        rows = assert_ok(response)["calendarEventAggregate"]
        # The events are at 09:00 UTC, which is 06:00 in São Paulo — same local
        # day, so three buckets of 2 / 1 / 1.
        assert [row["count"] for row in rows] == [2, 1, 1]
        assert all(row["key"]["startTime"].endswith("-03:00") for row in rows)

    def test_a_calendar_filter_narrows_the_aggregate(
        self, api_client, organization, calendar_a, calendar_b, events
    ):
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        response = post_graphql(
            api_client,
            EVENTS_BY_CALENDAR,
            credentials,
            event_window_variables(calendarId=calendar_b.id),
        )

        rows = assert_ok(response)["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == [calendar_b.id]

    def test_the_document_runs_one_grouped_statement(
        self, api_client, organization, calendar_a, calendar_b, events
    ):
        """No N+1: one GROUP BY, whatever the number of groups."""
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(
                api_client, EVENTS_BY_CALENDAR, credentials, event_window_variables()
            )

        assert len(assert_ok(response)["calendarEventAggregate"]) == 2
        grouped = [
            query["sql"]
            for query in captured.captured_queries
            if "GROUP BY" in query["sql"].upper()
            and "calendar_integration_calendarevent" in query["sql"]
        ]
        assert len(grouped) == 1, grouped

    def test_the_default_order_is_the_group_key(
        self, api_client, organization, calendar_a, calendar_b, events
    ):
        """Stable paging needs a deterministic ORDER BY, not the planner's whim."""
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        response = post_graphql(
            api_client, EVENTS_BY_CALENDAR, credentials, event_window_variables()
        )

        ids = [row["key"]["calendarId"] for row in assert_ok(response)["calendarEventAggregate"]]
        assert ids == sorted(ids)

    def test_limit_and_offset_page_the_groups(
        self, api_client, organization, calendar_a, calendar_b, events
    ):
        query = """
        query Paged($filter: CalendarEventAggregateFilterInput!, $offset: Int!) {
            calendarEventAggregate(
                filter: $filter
                groupBy: [{scalar: CALENDAR_ID}]
                timezone: "UTC"
                limit: 1
                offset: $offset
            ) {
                key { calendarId }
            }
        }
        """
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        first = assert_ok(
            post_graphql(api_client, query, credentials, {**event_window_variables(), "offset": 0})
        )["calendarEventAggregate"]
        second = assert_ok(
            post_graphql(api_client, query, credentials, {**event_window_variables(), "offset": 1})
        )["calendarEventAggregate"]

        assert [row["key"]["calendarId"] for row in first] == [min(calendar_a.id, calendar_b.id)]
        assert [row["key"]["calendarId"] for row in second] == [max(calendar_a.id, calendar_b.id)]

    def test_a_relation_count_comes_back_as_an_integer(
        self, api_client, organization, calendar_a, events
    ):
        query = """
        query Relations($filter: CalendarEventAggregateFilterInput!) {
            calendarEventAggregate(
                filter: $filter
                groupBy: [{scalar: CALENDAR_ID}]
                timezone: "UTC"
            ) {
                count
                attendancesCount
            }
        }
        """
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        response = post_graphql(api_client, query, credentials, event_window_variables())

        rows = assert_ok(response)["calendarEventAggregate"]
        assert all(row["attendancesCount"] == 0 for row in rows)

    def test_a_range_with_no_rows_returns_an_empty_list(
        self, api_client, organization, calendar_a, events
    ):
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = {
            "filter": {
                "startDatetime": datetime.datetime(2026, 5, 1, tzinfo=datetime.UTC).isoformat(),
                "endDatetime": datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC).isoformat(),
            }
        }

        response = post_graphql(api_client, EVENTS_BY_CALENDAR, credentials, variables)

        assert assert_ok(response)["calendarEventAggregate"] == []


@pytest.mark.django_db
class TestTheOtherFiveEntities:
    def test_available_time_aggregate(self, api_client, organization):
        calendar = make_calendar(organization, "Availability")
        make_available_time(organization, calendar, day=2)
        make_available_time(organization, calendar, day=3)
        query = """
        query Availability($filter: AvailableTimeAggregateFilterInput!) {
            availableTimeAggregate(
                filter: $filter
                groupBy: [{scalar: CALENDAR_ID}]
                timezone: "UTC"
            ) {
                key { calendarId }
                count
                durationMinutes { sum }
            }
        }
        """
        credentials = org_wide_token(organization, [PublicAPIResources.AVAILABLE_TIME])

        response = post_graphql(api_client, query, credentials, event_window_variables())

        assert assert_ok(response)["availableTimeAggregate"] == [
            {
                "key": {"calendarId": calendar.id},
                "count": 2,
                "durationMinutes": {"sum": 960.0},
            }
        ]

    def test_blocked_time_aggregate(self, api_client, organization):
        calendar = make_calendar(organization, "Blocks")
        make_blocked_time(organization, calendar, day=2)
        make_blocked_time(organization, calendar, day=3)
        query = """
        query Blocks($filter: BlockedTimeAggregateFilterInput!) {
            blockedTimeAggregate(
                filter: $filter
                groupBy: [{scalar: CALENDAR_ID}]
                timezone: "UTC"
            ) {
                count
                reason { min concat }
                durationMinutes { sum }
            }
        }
        """
        credentials = org_wide_token(organization, [PublicAPIResources.BLOCKED_TIME])

        response = post_graphql(api_client, query, credentials, event_window_variables())

        assert assert_ok(response)["blockedTimeAggregate"] == [
            {
                "count": 2,
                "reason": {"min": "Lunch", "concat": "Lunch,Lunch"},
                "durationMinutes": {"sum": 120.0},
            }
        ]

    def test_appointment_type_aggregate(self, api_client, organization):
        make_appointment_type(organization, name="Consult")
        make_appointment_type(organization, name="Follow-up")
        query = """
        query Types($filter: AppointmentTypeAggregateFilterInput!) {
            appointmentTypeAggregate(
                filter: $filter
                groupBy: [{scalar: ACCEPTS_PUBLIC_SCHEDULING}]
                timezone: "UTC"
            ) {
                key { acceptsPublicScheduling }
                count
                name { concat(separator: "; ") }
                durationMinutes { sum avg }
            }
        }
        """
        credentials = org_wide_token(organization, [PublicAPIResources.APPOINTMENT_TYPE])

        response = post_graphql(api_client, query, credentials, {"filter": {}})

        assert assert_ok(response)["appointmentTypeAggregate"] == [
            {
                "key": {"acceptsPublicScheduling": False},
                "count": 2,
                "name": {"concat": "Consult; Follow-up"},
                "durationMinutes": {"sum": 90.0, "avg": 45.0},
            }
        ]

    def test_calendar_aggregate_counts_relations_without_fanning_out(
        self, api_client, organization
    ):
        """Two events and two blocked times on one calendar means 2 and 2."""
        calendar = make_calendar(organization, "Busy")
        make_event(organization, calendar, title="One", day=2, minutes=30)
        make_event(organization, calendar, title="Two", day=3, minutes=30)
        make_blocked_time(organization, calendar, day=2)
        make_blocked_time(organization, calendar, day=3)
        query = """
        query Calendars($filter: CalendarAggregateFilterInput!) {
            calendarAggregate(
                filter: $filter
                groupBy: [{scalar: PROVIDER}]
                timezone: "UTC"
            ) {
                key { provider }
                count
                eventsCount
                blockedTimesCount
            }
        }
        """
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR])

        response = post_graphql(api_client, query, credentials, {"filter": {}})

        assert assert_ok(response)["calendarAggregate"] == [
            {
                "key": {"provider": "google"},
                "count": 1,
                "eventsCount": 2,
                "blockedTimesCount": 2,
            }
        ]

    def test_calendar_pool_aggregate(self, api_client, organization):
        make_calendar_pool(organization, name="Nurses")
        make_calendar_pool(organization, name="Rooms")
        query = """
        query Pools($filter: CalendarPoolAggregateFilterInput!) {
            calendarPoolAggregate(
                filter: $filter
                groupBy: [{temporal: {field: CREATED, granularity: MONTH}}]
                timezone: "UTC"
            ) {
                count
                name { concat(separator: "; ") }
                membershipsCount
            }
        }
        """
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_POOL])

        response = post_graphql(api_client, query, credentials, {"filter": {}})

        rows = assert_ok(response)["calendarPoolAggregate"]
        assert len(rows) == 1
        assert rows[0]["count"] == 2
        assert rows[0]["name"]["concat"] == "Nurses; Rooms"
        assert rows[0]["membershipsCount"] == 0


@pytest.mark.django_db
class TestAggregatesAgreeWithTheListField:
    def test_the_count_matches_the_rows_a_list_read_would_return(
        self, api_client, organization, calendar_a, calendar_b, events
    ):
        """An aggregate that disagreed with the list field would be the silent bug."""
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        response = post_graphql(
            api_client, EVENTS_BY_CALENDAR, credentials, event_window_variables()
        )

        rows = _rows_by_calendar(assert_ok(response)["calendarEventAggregate"])
        for calendar in (calendar_a, calendar_b):
            expected = CalendarEvent.objects.filter_by_organization(organization.id).filter(
                calendar_fk_id=calendar.id
            )
            assert rows[calendar.id]["count"] == expected.count()
