"""ORDER BY, exercised end-to-end through the schema.

Two properties matter: ordering by a metric returns groups in the requested
order with ``limit`` taking the top N, and repeated runs return the exact
same page -- the group-key tiebreak the executor already adds for every plan
(``_ordering_terms``) is what keeps ties from reshuffling between requests.
"""

import datetime
import uuid

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import Calendar
from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


CALENDAR_EVENT_ORDER_QUERY = """
query Agg(
    $filter: CalendarEventAggregateFilterInput!
    $groupBy: [CalendarEventGroupByInput!]!
    $orderBy: [CalendarEventAggregateOrderInput!]
    $limit: Int = 100
) {
    calendarEventAggregate(
        filter: $filter
        groupBy: $groupBy
        timezone: "UTC"
        orderBy: $orderBy
        limit: $limit
    ) {
        key { calendarId }
        count
    }
}
"""


@pytest.mark.django_db
class TestMetricOrderingExecution:
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

    def _variables(self, order_by: list[dict] | None, limit: int = 100) -> dict:
        return {
            "filter": {
                "startDatetime": "2026-01-01T00:00:00Z",
                "endDatetime": "2026-01-20T00:00:00Z",
                "calendarId": None,
            },
            "groupBy": [{"field": "CALENDAR_ID"}],
            "orderBy": order_by,
            "limit": limit,
        }

    def test_ordering_by_count_descending_with_a_limit_takes_the_top_n(self):
        org = self._org()
        busiest = self._make_calendar(org)
        middle = self._make_calendar(org)
        quietest = self._make_calendar(org)
        fourth = self._make_calendar(org)
        self._make_events(org, busiest, 5)
        self._make_events(org, middle, 3)
        self._make_events(org, quietest, 1)
        self._make_events(org, fourth, 2)
        system_user, token, auth = self._token(org)

        variables = self._variables(order_by=[{"metric": "COUNT", "direction": "DESC"}], limit=3)
        response = self._post(CALENDAR_EVENT_ORDER_QUERY, system_user, token, auth, variables)

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        rows = data["data"]["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == [
            busiest.id,
            middle.id,
            fourth.id,
        ]
        assert [row["count"] for row in rows] == [5, 3, 2]

    def test_ordering_by_count_ascending_reverses_the_order(self):
        org = self._org()
        busy = self._make_calendar(org)
        quiet = self._make_calendar(org)
        self._make_events(org, busy, 4)
        self._make_events(org, quiet, 1)
        system_user, token, auth = self._token(org)

        variables = self._variables(order_by=[{"metric": "COUNT", "direction": "ASC"}])
        response = self._post(CALENDAR_EVENT_ORDER_QUERY, system_user, token, auth, variables)

        assert response.status_code == 200
        data = response.json()
        rows = data["data"]["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == [quiet.id, busy.id]

    def test_ties_break_deterministically_across_repeated_runs(self):
        org = self._org()
        calendars = [self._make_calendar(org) for _ in range(4)]
        for calendar in calendars:
            self._make_events(org, calendar, 1)
        system_user, token, auth = self._token(org)

        variables = self._variables(order_by=[{"metric": "COUNT", "direction": "DESC"}])

        first = self._post(CALENDAR_EVENT_ORDER_QUERY, system_user, token, auth, variables)
        second = self._post(CALENDAR_EVENT_ORDER_QUERY, system_user, token, auth, variables)

        first_ids = [
            row["key"]["calendarId"] for row in first.json()["data"]["calendarEventAggregate"]
        ]
        second_ids = [
            row["key"]["calendarId"] for row in second.json()["data"]["calendarEventAggregate"]
        ]
        assert first_ids == second_ids
        # Every tied group ordered by its own key, ascending -- see the
        # executor's ``_ordering_terms`` tiebreak.
        assert first_ids == sorted(calendar.id for calendar in calendars)

    def test_ordering_by_a_grouped_key_orders_by_that_key(self):
        org = self._org()
        early = self._make_calendar(org)
        late = self._make_calendar(org)
        self._make_events(org, late, 1)
        self._make_events(org, early, 1)
        system_user, token, auth = self._token(org)

        variables = self._variables(
            order_by=[{"key": {"field": "CALENDAR_ID"}, "direction": "ASC"}]
        )
        response = self._post(CALENDAR_EVENT_ORDER_QUERY, system_user, token, auth, variables)

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        rows = data["data"]["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == sorted(
            calendar.id for calendar in (early, late)
        )

    def test_ordering_by_a_key_not_grouped_by_is_rejected(self):
        org = self._org()
        self._make_calendar(org)
        system_user, token, auth = self._token(org)

        variables = self._variables(
            order_by=[{"key": {"field": "IS_RECURRING_EXCEPTION"}, "direction": "ASC"}]
        )
        response = self._post(CALENDAR_EVENT_ORDER_QUERY, system_user, token, auth, variables)

        assert response.status_code == 200
        data = response.json()
        errors = data.get("errors") or []
        assert errors, "expected a refusal for ordering by an ungrouped dimension"
        assert data.get("data") in (None, {})

    def test_omitting_order_by_still_returns_results(self):
        org = self._org()
        calendar = self._make_calendar(org)
        self._make_events(org, calendar, 1)
        system_user, token, auth = self._token(org)

        response = self._post(
            CALENDAR_EVENT_ORDER_QUERY, system_user, token, auth, self._variables(order_by=None)
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["calendarEventAggregate"][0]["key"]["calendarId"] == calendar.id
