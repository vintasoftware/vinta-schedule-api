"""End-to-end aggregates through the published schema, for all six entities.

Each test posts a real document to `/graphql/` with a real token and asserts
the numbers, not that something came back. The worked example is the one the
plan's acceptance criterion names: `calendarEventAggregate` grouped by calendar,
returning a correct `count` and correct `durationMinutes { sum avg }`.
"""

import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarType
from calendar_integration.factories import create_calendar_pool
from calendar_integration.models import AppointmentType
from public_api.constants import PublicAPIResources
from public_api.tests.aggregations.conftest import (
    WINDOW_END,
    WINDOW_START,
    make_available_time,
    make_blocked_time,
    make_calendar,
    make_event,
    org_wide_token,
    post_graphql,
)


EVENT_AGGREGATE_QUERY = """
query EventAggregate($start: DateTime!, $end: DateTime!) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{scalar: CALENDAR_ID}]
    timezone: "UTC"
  ) {
    key { calendarId }
    count
    durationMinutes { sum avg min max }
  }
}
"""

EVENT_CONCAT_QUERY = """
query EventConcat($start: DateTime!, $end: DateTime!) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{scalar: CALENDAR_ID}]
    timezone: "UTC"
  ) {
    key { calendarId }
    count
    title { concat(separator: "; ") min max }
  }
}
"""

EVENT_DAY_BUCKET_QUERY = """
query EventPerDay($start: DateTime!, $end: DateTime!, $tz: String!) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
    timezone: $tz
  ) {
    key { startTime }
    count
  }
}
"""


def _window() -> dict[str, str]:
    return {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()}


@pytest.mark.django_db
class TestCalendarEventAggregate:
    """The plan's acceptance case, in full."""

    def test_grouped_by_calendar_returns_correct_count_and_numeric_rollups(self, organization):
        calendar_a = make_calendar(organization, name="A")
        calendar_b = make_calendar(organization, name="B")
        base = datetime.datetime(2026, 3, 10, 9, 0)

        # Calendar A: 30 + 90 minutes. Calendar B: 60 minutes.
        make_event(organization, calendar_a, title="A1", start=base, minutes=30)
        make_event(
            organization,
            calendar_a,
            title="A2",
            start=base + datetime.timedelta(days=1),
            minutes=90,
        )
        make_event(organization, calendar_b, title="B1", start=base, minutes=60)

        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        response = post_graphql(EVENT_AGGREGATE_QUERY, system_user, token, auth, _window())

        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        rows = {row["key"]["calendarId"]: row for row in payload["data"]["calendarEventAggregate"]}
        assert set(rows) == {calendar_a.id, calendar_b.id}

        assert rows[calendar_a.id]["count"] == 2
        assert rows[calendar_a.id]["durationMinutes"]["sum"] == pytest.approx(120.0)
        assert rows[calendar_a.id]["durationMinutes"]["avg"] == pytest.approx(60.0)
        assert rows[calendar_a.id]["durationMinutes"]["min"] == pytest.approx(30.0)
        assert rows[calendar_a.id]["durationMinutes"]["max"] == pytest.approx(90.0)

        assert rows[calendar_b.id]["count"] == 1
        assert rows[calendar_b.id]["durationMinutes"]["sum"] == pytest.approx(60.0)
        assert rows[calendar_b.id]["durationMinutes"]["avg"] == pytest.approx(60.0)

    def test_concat_returns_the_concatenated_titles(self, organization):
        """`title { concat(separator: "; ") }` is Postgres `string_agg`, ordered."""
        calendar = make_calendar(organization)
        base = datetime.datetime(2026, 3, 12, 9, 0)
        make_event(organization, calendar, title="Alpha", start=base, minutes=30)
        make_event(
            organization,
            calendar,
            title="Bravo",
            start=base + datetime.timedelta(hours=2),
            minutes=30,
        )

        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        response = post_graphql(EVENT_CONCAT_QUERY, system_user, token, auth, _window())

        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        (row,) = payload["data"]["calendarEventAggregate"]
        # `_string_agg` orders by the aggregated value, so the string is stable.
        assert row["title"]["concat"] == "Alpha; Bravo"
        assert row["title"]["min"] == "Alpha"
        assert row["title"]["max"] == "Bravo"

    def test_grouped_by_day_buckets_on_the_caller_timezone(self, organization):
        """Two events an hour apart across a Sao Paulo midnight are two days."""
        calendar = make_calendar(organization)
        # 02:30 UTC on 2026-03-12 is 23:30 on 2026-03-11 in Sao Paulo (UTC-3).
        before_midnight = datetime.datetime(2026, 3, 12, 2, 30)
        after_midnight = datetime.datetime(2026, 3, 12, 4, 30)
        make_event(organization, calendar, title="Late", start=before_midnight, minutes=30)
        make_event(organization, calendar, title="Early", start=after_midnight, minutes=30)

        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"tz": "America/Sao_Paulo"}
        response = post_graphql(EVENT_DAY_BUCKET_QUERY, system_user, token, auth, variables)

        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        rows = payload["data"]["calendarEventAggregate"]
        assert len(rows) == 2
        assert [row["count"] for row in rows] == [1, 1]

    def test_document_runs_one_aggregate_query(self, organization):
        """The whole grouped result is one `GROUP BY` round trip.

        Counted as "queries that carry a GROUP BY" rather than as every query on
        the connection: authenticating the token and resolving its resources are
        the endpoint's own reads, and they are not what this guards against.
        """
        calendar = make_calendar(organization)
        base = datetime.datetime(2026, 3, 14, 9, 0)
        for index in range(4):
            make_event(
                organization,
                calendar,
                title=f"E{index}",
                start=base + datetime.timedelta(hours=index),
                minutes=30,
            )

        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(EVENT_AGGREGATE_QUERY, system_user, token, auth, _window())

        assert response.status_code == 200
        assert response.json().get("errors", []) == []
        grouped = [query for query in captured.captured_queries if "GROUP BY" in query["sql"]]
        assert len(grouped) == 1, [query["sql"] for query in grouped]


@pytest.mark.django_db
class TestEverySpanEntityAggregates:
    def test_available_time_aggregate(self, organization):
        calendar = make_calendar(organization)
        base = datetime.datetime(2026, 3, 16, 9, 0)
        make_available_time(organization, calendar, start=base, minutes=60)
        make_available_time(
            organization, calendar, start=base + datetime.timedelta(days=1), minutes=120
        )

        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.AVAILABLE_TIME])
        query = """
        query AvailableTimeAggregate($start: DateTime!, $end: DateTime!) {
          availableTimeAggregate(
            filter: {startDatetime: $start, endDatetime: $end}
            groupBy: [{scalar: CALENDAR_ID}]
            timezone: "UTC"
          ) {
            key { calendarId }
            count
            durationMinutes { sum }
          }
        }
        """
        response = post_graphql(query, system_user, token, auth, _window())
        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        (row,) = payload["data"]["availableTimeAggregate"]
        assert row["key"]["calendarId"] == calendar.id
        assert row["count"] == 2
        assert row["durationMinutes"]["sum"] == pytest.approx(180.0)

    def test_blocked_time_aggregate(self, organization):
        calendar = make_calendar(organization)
        base = datetime.datetime(2026, 3, 18, 9, 0)
        make_blocked_time(organization, calendar, reason="Lunch", start=base, minutes=45)
        make_blocked_time(
            organization,
            calendar,
            reason="Focus",
            start=base + datetime.timedelta(days=1),
            minutes=45,
        )

        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.BLOCKED_TIME])
        query = """
        query BlockedTimeAggregate($start: DateTime!, $end: DateTime!) {
          blockedTimeAggregate(
            filter: {startDatetime: $start, endDatetime: $end}
            groupBy: [{scalar: CALENDAR_ID}]
            timezone: "UTC"
          ) {
            key { calendarId }
            count
            reason { concat(separator: ", ") }
          }
        }
        """
        response = post_graphql(query, system_user, token, auth, _window())
        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        (row,) = payload["data"]["blockedTimeAggregate"]
        assert row["count"] == 2
        assert row["reason"]["concat"] == "Focus, Lunch"


@pytest.mark.django_db
class TestNonTemporalEntitiesAggregate:
    def test_appointment_type_aggregate(self, organization):
        baker.make(
            AppointmentType,
            organization=organization,
            name="Intake",
            accepts_public_scheduling=True,
            duration=datetime.timedelta(minutes=30),
        )
        baker.make(
            AppointmentType,
            organization=organization,
            name="Review",
            accepts_public_scheduling=True,
            duration=datetime.timedelta(minutes=90),
        )

        system_user, token, auth = org_wide_token(
            organization, [PublicAPIResources.APPOINTMENT_TYPE]
        )
        query = """
        query AppointmentTypeAggregate {
          appointmentTypeAggregate(
            filter: {}
            groupBy: [{scalar: ACCEPTS_PUBLIC_SCHEDULING}]
            timezone: "UTC"
          ) {
            key { acceptsPublicScheduling }
            count
            durationMinutes { sum avg }
          }
        }
        """
        response = post_graphql(query, system_user, token, auth, {})
        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        (row,) = payload["data"]["appointmentTypeAggregate"]
        assert row["key"]["acceptsPublicScheduling"] is True
        assert row["count"] == 2
        assert row["durationMinutes"]["sum"] == pytest.approx(120.0)
        assert row["durationMinutes"]["avg"] == pytest.approx(60.0)

    def test_calendar_aggregate(self, organization):
        make_calendar(organization)
        make_calendar(organization)

        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR])
        query = """
        query CalendarAggregate {
          calendarAggregate(
            filter: {}
            groupBy: [{scalar: CALENDAR_TYPE}]
            timezone: "UTC"
          ) {
            key { calendarType }
            count
            name { concat(separator: "|") }
          }
        }
        """
        response = post_graphql(query, system_user, token, auth, {})
        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        (row,) = payload["data"]["calendarAggregate"]
        assert row["key"]["calendarType"] == CalendarType.PERSONAL
        assert row["count"] == 2

    def test_calendar_pool_aggregate(self, organization):
        """A pool has no scalar dimension, so this is the temporal-only path."""
        calendar = make_calendar(organization)
        create_calendar_pool(organization=organization, name="Pool A", calendars=[calendar])
        create_calendar_pool(organization=organization, name="Pool B", calendars=[])

        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_POOL])
        query = """
        query CalendarPoolAggregate {
          calendarPoolAggregate(
            filter: {}
            groupBy: [{temporal: {field: CREATED, granularity: MONTH}}]
            timezone: "UTC"
          ) {
            key { created }
            count
            membershipCount
          }
        }
        """
        response = post_graphql(query, system_user, token, auth, {})
        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        (row,) = payload["data"]["calendarPoolAggregate"]
        assert row["count"] == 2
        # One pool has a single roster calendar, the other none: counted through
        # a correlated subquery, so the two do not multiply each other.
        assert row["membershipCount"] == 1
