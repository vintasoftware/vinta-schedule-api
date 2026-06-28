"""End-to-end tests for the ``calendarBookableSlots`` public GraphQL query (Phase 5).

Asserts the query is org-scoped, resource-gated under ``BOOKABLE_SLOTS``, and
returns discretized slots for a single calendar (the integration / no-policy path
is covered exhaustively in
``calendar_integration/tests/services/test_bookable_slots_service.py``).
"""

import datetime
import json
from unittest.mock import patch

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import AvailableTime, Calendar
from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


_QUERY = """
    query Slots($calendarId: Int!, $start: DateTime!, $end: DateTime!,
                $duration: Int!, $step: Int!) {
        calendarBookableSlots(
            calendarId: $calendarId,
            searchWindowStart: $start,
            searchWindowEnd: $end,
            durationSeconds: $duration,
            slotStepSeconds: $step
        ) {
            startTime
            endTime
        }
    }
"""


@pytest.fixture
def organization():
    return baker.make(Organization, name="Slots Query Org", should_sync_rooms=False)


def _client_with_resources(organization, resources):
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name="slots_integration", organization=organization
    )
    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client


def _managed_calendar(organization):
    return Calendar.objects.create(
        organization=organization,
        name="Slots cal",
        external_id="slots-cal",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=True,
    )


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestCalendarBookableSlotsQuery:
    def _post(self, client, calendar_id, start, end):
        variables = {
            "calendarId": calendar_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "duration": 30 * 60,
            "step": 30 * 60,
        }
        return client.post(
            "/graphql/",
            data=json.dumps({"query": _QUERY, "variables": variables}),
            content_type="application/json",
        )

    def test_returns_slots_for_authorized_org_scoped_caller(self, mock_rl, organization):
        mock_rl.return_value = iter([None])
        cal = _managed_calendar(organization)
        start = datetime.datetime(2026, 9, 2, 9, 0, tzinfo=datetime.UTC)
        end = start + datetime.timedelta(hours=1)
        AvailableTime.objects.create(
            organization=organization,
            calendar=cal,
            start_time_tz_unaware=start,
            end_time_tz_unaware=end,
            timezone="UTC",
        )
        client = _client_with_resources(organization, [PublicAPIResources.BOOKABLE_SLOTS])

        response = self._post(client, cal.id, start, end)

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data
        slots = data["data"]["calendarBookableSlots"]
        mid = start + datetime.timedelta(minutes=30)
        assert [(s["startTime"], s["endTime"]) for s in slots] == [
            (start.isoformat(), mid.isoformat()),
            (mid.isoformat(), end.isoformat()),
        ]

    def test_token_without_bookable_slots_resource_is_denied(self, mock_rl, organization):
        mock_rl.return_value = iter([None])
        cal = _managed_calendar(organization)
        start = datetime.datetime(2026, 9, 2, 9, 0, tzinfo=datetime.UTC)
        end = start + datetime.timedelta(hours=1)
        # Grant a DIFFERENT resource, not BOOKABLE_SLOTS.
        client = _client_with_resources(organization, [PublicAPIResources.CALENDAR])

        response = self._post(client, cal.id, start, end)

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data, "Token without BOOKABLE_SLOTS must get a permission error"
        assert (data.get("data") or {}).get("calendarBookableSlots") is None

    def test_cross_org_calendar_not_visible(self, mock_rl, organization):
        mock_rl.return_value = iter([None])
        other_org = baker.make(Organization, name="Other Slots Org", should_sync_rooms=False)
        other_cal = _managed_calendar(other_org)
        start = datetime.datetime(2026, 9, 2, 9, 0, tzinfo=datetime.UTC)
        end = start + datetime.timedelta(hours=1)
        client = _client_with_resources(organization, [PublicAPIResources.BOOKABLE_SLOTS])

        # Asking for a calendar in another org → org filter raises DoesNotExist →
        # GraphQL error, no slots leaked.
        response = self._post(client, other_cal.id, start, end)

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert (data.get("data") or {}).get("calendarBookableSlots") is None
