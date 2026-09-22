"""A nested aggregate costs the aggregated entity's resource, not the parent's.

The whole risk of making aggregates reachable under a parent is that the
nesting becomes a way around a scope: a token granted ``calendar`` alone can
already read a calendar, and if ``calendar.eventAggregate`` rode on that grant
it would be reading events it was never given. So the resource a nested field
checks is the one its *rows* come from, and reading the parent has to keep
working while the rollup under it is refused.

Tenant scoping is the second half and is checked here too: the batched query
covers every parent at its level at once, so a filter that let one
organization's rows into another's batch would be the worst bug this feature
could have.
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
from public_api.permissions import OrganizationResourceAccess
from public_api.services import PublicAPIAuthService


NESTED_QUERY = """
query Nested(
    $filter: CalendarEventAggregateFilterInput!
    $groupBy: [CalendarEventGroupByInput!]!
) {
    calendars(limit: 100) {
        id
        name
        eventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
            key { calendarId }
            count
        }
    }
}
"""

PARENT_ONLY_QUERY = """
query ParentOnly {
    calendars(limit: 100) {
        id
        name
    }
}
"""


class TestNestedAggregateResourceMapping:
    """The mapping itself, before any request is made."""

    def test_event_aggregate_requires_the_event_resource(self):
        mapping = OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING

        assert mapping["eventAggregate"] == PublicAPIResources.CALENDAR_EVENT

    def test_blocked_time_aggregate_requires_the_blocked_time_resource(self):
        """The nested field and the root field share a GraphQL name, so they
        share this entry -- which is the point, not a coincidence."""
        mapping = OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING

        assert mapping["blockedTimeAggregate"] == PublicAPIResources.BLOCKED_TIME

    def test_no_nested_aggregate_falls_back_to_its_parents_resource(self):
        """Every nested aggregate field name is mapped explicitly.

        An unmapped name falls through to ``info.field_name`` itself, which no
        token is ever granted -- so the failure would be a refusal rather than
        a leak. It would still be the wrong refusal, and silent.
        """
        mapping = OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING

        for field_name in ("eventAggregate", "blockedTimeAggregate"):
            assert field_name in mapping
            assert mapping[field_name] != PublicAPIResources.CALENDAR
            assert mapping[field_name] != PublicAPIResources.CALENDAR_POOL
            assert mapping[field_name] != PublicAPIResources.APPOINTMENT_TYPE


@pytest.mark.django_db
class TestNestedAggregatePermissions:
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

    def _make_event(self, org, calendar):
        return baker.make(
            "calendar_integration.CalendarEvent",
            organization=org,
            calendar=calendar,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5, 10, 0, 0),
            end_time_tz_unaware=datetime.datetime(2026, 1, 5, 11, 0, 0),
            timezone="UTC",
            external_id=f"e-{uuid.uuid4().hex[:8]}",
            title="Event",
        )

    def _token(self, org: Organization, *resources: str):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"orgwide_{uuid.uuid4().hex[:8]}", organization=org
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _post(self, query, system_user, token, auth_service, variables=None):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": query, "variables": variables or {}},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _variables(self) -> dict:
        return {
            "filter": {
                "startDatetime": "2026-01-01T00:00:00Z",
                "endDatetime": "2026-01-20T00:00:00Z",
                "calendarId": None,
            },
            "groupBy": [{"field": "CALENDAR_ID"}],
        }

    # -- the load-bearing case -----------------------------------------

    def test_a_calendar_only_token_reads_the_calendar_and_not_its_event_rollup(self):
        org = self._org()
        calendar = self._make_calendar(org)
        self._make_event(org, calendar)
        system_user, token, auth = self._token(org, PublicAPIResources.CALENDAR)

        response = self._post(NESTED_QUERY, system_user, token, auth, self._variables())

        assert response.status_code == 200
        payload = response.json()
        # Refused, and refused for the reason the permission class gives.
        assert payload.get("errors")
        assert any(
            "don't have access to query this resource" in error["message"]
            for error in payload["errors"]
        )
        # The parent itself is still readable with the same token, in the same
        # document: the refusal is the rollup's, not the calendar's.
        parent_only = self._post(PARENT_ONLY_QUERY, system_user, token, auth)
        parent_payload = parent_only.json()
        assert parent_payload.get("errors", []) == []
        assert [row["id"] for row in parent_payload["data"]["calendars"]] == [str(calendar.id)]

    def test_both_resources_together_are_what_read_the_rollup(self):
        org = self._org()
        calendar = self._make_calendar(org)
        self._make_event(org, calendar)
        system_user, token, auth = self._token(
            org, PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT
        )

        response = self._post(NESTED_QUERY, system_user, token, auth, self._variables())

        payload = response.json()
        assert payload.get("errors", []) == []
        (row,) = payload["data"]["calendars"]
        assert row["eventAggregate"][0]["count"] == 1
        assert row["eventAggregate"][0]["key"]["calendarId"] == calendar.id

    def test_the_event_resource_alone_does_not_reach_the_nested_field(self):
        """The parent list has its own gate, and this does not remove it."""
        org = self._org()
        calendar = self._make_calendar(org)
        self._make_event(org, calendar)
        system_user, token, auth = self._token(org, PublicAPIResources.CALENDAR_EVENT)

        response = self._post(NESTED_QUERY, system_user, token, auth, self._variables())

        assert response.json().get("errors")

    def test_an_appointment_type_only_token_is_refused_the_nested_event_rollup(self):
        """Same rule under a different parent: the resource follows the rows."""
        org = self._org()
        baker.make(
            "calendar_integration.AppointmentType",
            organization=org,
            duration=datetime.timedelta(minutes=30),
        )
        system_user, token, auth = self._token(org, PublicAPIResources.APPOINTMENT_TYPE)
        query = """
        query Nested(
            $filter: CalendarEventAggregateFilterInput!
            $groupBy: [CalendarEventGroupByInput!]!
        ) {
            appointmentTypes(limit: 100) {
                id
                eventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                    count
                }
            }
        }
        """

        response = self._post(query, system_user, token, auth, self._variables())

        assert response.json().get("errors")

    def test_a_pool_only_token_is_refused_the_nested_event_rollup(self):
        org = self._org()
        baker.make("calendar_integration.CalendarPool", organization=org, name="Pool")
        system_user, token, auth = self._token(org, PublicAPIResources.CALENDAR_POOL)
        query = """
        query Nested(
            $filter: CalendarEventAggregateFilterInput!
            $groupBy: [CalendarEventGroupByInput!]!
        ) {
            calendarPools(limit: 100) {
                id
                eventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                    count
                }
            }
        }
        """

        response = self._post(query, system_user, token, auth, self._variables())

        assert response.json().get("errors")

    # -- tenancy -------------------------------------------------------

    def test_the_batch_never_carries_another_organizations_rows(self):
        """One batched query covers a whole level, so this is the bug to fear.

        The other organization's calendar has three events to the caller's
        one; if the batch were unscoped the count would come back as four, or
        a second row would appear for a calendar the caller cannot see.
        """
        org = self._org()
        other_org = self._org()
        mine = self._make_calendar(org)
        self._make_event(org, mine)
        theirs = self._make_calendar(other_org)
        for _ in range(3):
            self._make_event(other_org, theirs)
        system_user, token, auth = self._token(
            org, PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT
        )

        response = self._post(NESTED_QUERY, system_user, token, auth, self._variables())

        payload = response.json()
        assert payload.get("errors", []) == []
        calendars = payload["data"]["calendars"]
        assert [row["id"] for row in calendars] == [str(mine.id)]
        (row,) = calendars
        assert row["eventAggregate"][0]["count"] == 1
        assert row["eventAggregate"][0]["key"]["calendarId"] == mine.id
