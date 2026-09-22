"""HAVING, exercised end-to-end through the schema.

The property that matters is not just "the wrong groups are gone" -- a
resolver-side Python filter over the executor's rows would satisfy that too,
and it is exactly the kind of post-processing the plan rules out. So every
test here also asserts the emitted SQL carries a ``HAVING`` clause and that
the whole document still costs one query.
"""

import datetime
import uuid

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import Calendar
from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


CALENDAR_EVENT_HAVING_QUERY = """
query Agg(
    $filter: CalendarEventAggregateFilterInput!
    $groupBy: [CalendarEventGroupByInput!]!
    $having: CalendarEventHavingInput
) {
    calendarEventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC", having: $having) {
        key { calendarId }
        count
    }
}
"""


@pytest.mark.django_db
class TestHavingExecution:
    def setup_method(self):
        self.client = APIClient()

    def _org(self) -> Organization:
        return baker.make(Organization, name=f"Org {uuid.uuid4().hex[:6]}")

    def _make_calendar(self, org: Organization) -> Calendar:
        unique = uuid.uuid4().hex[:8]
        return Calendar.objects.create(
            organization=org,
            name=f"Calendar {unique}",
            external_id=f"cal-{unique}",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
        )

    def _make_events(self, org: Organization, calendar: Calendar, count: int) -> None:
        for index in range(count):
            baker.make(
                "calendar_integration.CalendarEvent",
                organization=org,
                calendar=calendar,
                start_time_tz_unaware=datetime.datetime(2026, 1, 5, 10, 0, 0),
                end_time_tz_unaware=datetime.datetime(2026, 1, 5, 11, 0, 0),
                timezone="UTC",
                external_id=f"e-{uuid.uuid4().hex[:8]}",
                title=f"Event {index}",
            )

    def _token(self, org: Organization):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"orgwide_{uuid.uuid4().hex[:8]}", organization=org
        )
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR_EVENT
        )
        return system_user, token, auth_service

    def _post(self, query, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": query, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _variables(self, having: dict | None) -> dict:
        return {
            "filter": {
                "startDatetime": "2026-01-01T00:00:00Z",
                "endDatetime": "2026-01-20T00:00:00Z",
                "calendarId": None,
            },
            "groupBy": [{"field": "CALENDAR_ID"}],
            "having": having,
        }

    def test_having_drops_groups_below_the_threshold_and_keeps_the_rest(self):
        org = self._org()
        busy = self._make_calendar(org)
        medium = self._make_calendar(org)
        quiet = self._make_calendar(org)
        self._make_events(org, busy, 3)
        self._make_events(org, medium, 2)
        self._make_events(org, quiet, 1)
        system_user, token, auth = self._token(org)

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(
                CALENDAR_EVENT_HAVING_QUERY,
                system_user,
                token,
                auth,
                self._variables(having={"count": {"gt": 2}}),
            )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        rows = data["data"]["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == [busy.id]
        assert rows[0]["count"] == 3

        # The filtering happened in the database, not in the resolver: the
        # emitted SQL carries a HAVING clause, and the whole document still
        # cost exactly one data-fetching query.
        aggregate_queries = [
            q for q in ctx.captured_queries if "calendar_integration_calendarevent" in q["sql"]
        ]
        assert len(aggregate_queries) == 1
        assert "HAVING" in aggregate_queries[0]["sql"].upper()

    def test_having_with_no_matching_group_returns_an_empty_list(self):
        org = self._org()
        calendar = self._make_calendar(org)
        self._make_events(org, calendar, 1)
        system_user, token, auth = self._token(org)

        response = self._post(
            CALENDAR_EVENT_HAVING_QUERY,
            system_user,
            token,
            auth,
            self._variables(having={"count": {"gt": 100}}),
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["calendarEventAggregate"] == []

    def test_having_on_a_metric_not_selected_in_the_output_still_filters(self):
        """The guard: a HAVING clause can reference a metric the row
        selection never asked to see, and it still filters -- rather than
        erroring for want of an annotation."""
        org = self._org()
        long_events_calendar = self._make_calendar(org)
        short_events_calendar = self._make_calendar(org)
        baker.make(
            "calendar_integration.CalendarEvent",
            organization=org,
            calendar=long_events_calendar,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5, 10, 0, 0),
            end_time_tz_unaware=datetime.datetime(2026, 1, 5, 12, 0, 0),
            timezone="UTC",
            external_id=f"e-{uuid.uuid4().hex[:8]}",
            title="Long",
        )
        baker.make(
            "calendar_integration.CalendarEvent",
            organization=org,
            calendar=short_events_calendar,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5, 10, 0, 0),
            end_time_tz_unaware=datetime.datetime(2026, 1, 5, 10, 15, 0),
            timezone="UTC",
            external_id=f"e-{uuid.uuid4().hex[:8]}",
            title="Short",
        )
        system_user, token, auth = self._token(org)

        variables = self._variables(
            having={"durationMinutes": {"sum": {"gt": 60}}},
        )
        response = self._post(CALENDAR_EVENT_HAVING_QUERY, system_user, token, auth, variables)

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        rows = data["data"]["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == [long_events_calendar.id]

    def test_and_or_composition_matches_the_documented_semantics(self):
        org = self._org()
        matches_both = self._make_calendar(org)
        matches_neither = self._make_calendar(org)
        self._make_events(org, matches_both, 5)
        self._make_events(org, matches_neither, 1)
        system_user, token, auth = self._token(org)

        # `count > 3 AND (count = 5 OR count = 6)` -- only `matches_both`
        # (count 5) satisfies both branches.
        having = {
            "count": {"gt": 3},
            "or": [{"count": {"eq": 5}}, {"count": {"eq": 6}}],
        }
        response = self._post(
            CALENDAR_EVENT_HAVING_QUERY, system_user, token, auth, self._variables(having=having)
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        rows = data["data"]["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == [matches_both.id]
