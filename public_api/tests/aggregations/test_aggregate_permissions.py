"""Aggregate fields are scoped exactly like the entity's list field.

Three properties, all through the schema (POST to ``/graphql/``, real
middleware, real ``permission_classes`` -- never a bare resolver call):

* a token without the entity's resource gets the standard
  ``OrganizationResourceAccess`` refusal;
* a token *with* the resource succeeds and reads real numbers;
* a token scoped to one organization's calendar gets zero contribution from
  another organization's events -- the cross-tenant case asserts an empty
  result, never another tenant's numbers (a ``GROUP BY`` over an
  insufficiently scoped queryset is the one bug in this feature that would
  not look like a bug in its output).
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


CALENDAR_EVENT_AGGREGATE_QUERY = """
query CalendarEventAggregate(
    $filter: CalendarEventAggregateFilterInput!
    $groupBy: [CalendarEventGroupByInput!]!
) {
    calendarEventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
        key {
            calendarId
        }
        count
    }
}
"""


@pytest.mark.django_db
class TestAggregatePermissions:
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

    def _make_event(self, org: Organization, calendar: Calendar, *, external_id: str):
        return baker.make(
            "calendar_integration.CalendarEvent",
            organization=org,
            calendar=calendar,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5, 10, 0, 0),
            end_time_tz_unaware=datetime.datetime(2026, 1, 5, 11, 0, 0),
            timezone="UTC",
            external_id=external_id,
        )

    def _org_wide_token(self, org: Organization, resources: list[str]):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"orgwide_{uuid.uuid4().hex[:8]}", organization=org
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
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

    def _variables(self, calendar_id: int | None = None) -> dict:
        return {
            "filter": {
                "startDatetime": "2026-01-01T00:00:00Z",
                "endDatetime": "2026-01-20T00:00:00Z",
                "calendarId": calendar_id,
            },
            "groupBy": [{"field": "CALENDAR_ID"}],
        }

    def test_token_without_resource_is_refused(self):
        org = self._org()
        calendar = self._make_calendar(org)
        self._make_event(org, calendar, external_id="e1")
        system_user, token, auth = self._org_wide_token(org, [])

        response = self._post(
            CALENDAR_EVENT_AGGREGATE_QUERY,
            system_user,
            token,
            auth,
            self._variables(),
        )

        assert response.status_code == 200
        data = response.json()
        errors = data.get("errors") or []
        assert errors, "expected a permission error for a token without CALENDAR_EVENT"
        assert any("access" in (err.get("message") or "").lower() for err in errors)

    def test_token_with_resource_succeeds(self):
        org = self._org()
        calendar = self._make_calendar(org)
        self._make_event(org, calendar, external_id="e1")
        self._make_event(org, calendar, external_id="e2")
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_EVENT])

        response = self._post(
            CALENDAR_EVENT_AGGREGATE_QUERY,
            system_user,
            token,
            auth,
            self._variables(),
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        rows = data["data"]["calendarEventAggregate"]
        assert len(rows) == 1
        assert rows[0]["key"]["calendarId"] == calendar.id
        assert rows[0]["count"] == 2

    def test_cross_tenant_events_contribute_nothing(self):
        """A token bound to org1 aggregates none of org2's events, even
        though org2's calendar shares no id collision protection other than
        the organization filter itself."""
        org1 = self._org()
        org2 = self._org()
        calendar1 = self._make_calendar(org1)
        calendar2 = self._make_calendar(org2)
        self._make_event(org1, calendar1, external_id="e1")
        self._make_event(org2, calendar2, external_id="e2")
        self._make_event(org2, calendar2, external_id="e3")

        system_user, token, auth = self._org_wide_token(org1, [PublicAPIResources.CALENDAR_EVENT])

        response = self._post(
            CALENDAR_EVENT_AGGREGATE_QUERY,
            system_user,
            token,
            auth,
            self._variables(),
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        rows = data["data"]["calendarEventAggregate"]
        # Only org1's calendar shows up; org2's two events contribute no row
        # and, just as important, do not inflate org1's calendar's count.
        assert len(rows) == 1
        assert rows[0]["key"]["calendarId"] == calendar1.id
        assert rows[0]["count"] == 1
