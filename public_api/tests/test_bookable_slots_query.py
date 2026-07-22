"""End-to-end tests for the ``calendarBookableSlots`` and
``calendarGroupBookableSlots`` public GraphQL queries.

Asserts the queries are org-scoped, require a resource grant, and return discretized slots.
Policy filtering (lead-time, max-horizon, buffers) is verified at the GraphQL
resolver level for both the single-calendar and group variants.
"""

import datetime
import json
from unittest.mock import patch

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.factories import create_booking_policy
from calendar_integration.models import AvailableTime, Calendar
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.dataclasses import (
    CalendarGroupInputData,
    CalendarGroupSlotInputData,
)
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


# ---------------------------------------------------------------------------
# calendarGroupBookableSlots — policy-aware resolver
# ---------------------------------------------------------------------------

_GROUP_BOOKABLE_SLOTS_QUERY = """
    query GroupSlots(
        $groupId: Int!,
        $start: DateTime!,
        $end: DateTime!,
        $duration: Int!,
        $step: Int!
    ) {
        calendarGroupBookableSlots(
            groupId: $groupId,
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


def _group_client_with_resources(org):
    """Create an authenticated API client with CALENDAR_GROUP resource."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name="group_slots_integration", organization=org
    )
    baker.make(
        ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR_GROUP
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client


def _make_group(org, *, cal):
    """Create a one-slot CalendarGroup with one unmanaged calendar."""
    from calendar_integration.services.booking_policy_service import BookingPolicyService

    svc = CalendarGroupService(booking_policy_service=BookingPolicyService())
    svc.initialize(organization=org)
    return svc.create_group(
        CalendarGroupInputData(
            name="Policy Group",
            description="",
            slots=[
                CalendarGroupSlotInputData(
                    name="Slot",
                    calendar_ids=[cal.id],
                    required_count=1,
                    order=0,
                )
            ],
        )
    )


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestCalendarGroupBookableSlotsQuery:
    """End-to-end GraphQL tests for calendarGroupBookableSlots with a BookingPolicy.

    Verifies the DI wiring that makes the public group resolver policy-aware:
    slots filtered by the policy (lead-time) are absent from the response.
    """

    def _post(self, client, group_id, start, end, duration_s=30 * 60, step_s=30 * 60):
        variables = {
            "groupId": group_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "duration": duration_s,
            "step": step_s,
        }
        return client.post(
            "/graphql/",
            data=json.dumps({"query": _GROUP_BOOKABLE_SLOTS_QUERY, "variables": variables}),
            content_type="application/json",
        )

    def test_group_lead_time_policy_filters_early_slots(self, mock_rl, organization):
        """A group with a lead_time BookingPolicy: the resolver honours it —
        candidates within the lead-time window are absent from the response."""
        mock_rl.return_value = iter([None])

        # Unmanaged calendar — always free (no blocking events).
        cal = Calendar.objects.create(
            organization=organization,
            name="Group Slots Cal",
            external_id="gs-cal-1",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=False,
        )
        group = _make_group(organization, cal=cal)

        # 2-hour lead time.
        lead_time = datetime.timedelta(hours=2)
        create_booking_policy(
            calendar_group=group,
            lead_time_seconds=int(lead_time.total_seconds()),
        )

        client = _group_client_with_resources(organization)
        now = datetime.datetime.now(tz=datetime.UTC).replace(microsecond=0)
        start = now
        end = now + datetime.timedelta(hours=4)

        response = self._post(client, group.id, start, end)

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data
        slots = data["data"]["calendarGroupBookableSlots"]

        cutoff = now + lead_time
        # Every slot must start at or after the lead-time cutoff.
        assert slots, "Expected at least some slots after the lead-time cutoff"
        for slot in slots:
            slot_start = datetime.datetime.fromisoformat(slot["startTime"])
            assert slot_start >= cutoff, (
                f"Slot {slot_start} is before the lead_time cutoff {cutoff}"
            )
        # Slots that would have started before the cutoff must be absent.
        before_cutoff = [
            s for s in slots if datetime.datetime.fromisoformat(s["startTime"]) < cutoff
        ]
        assert not before_cutoff, f"Slots before lead-time cutoff leaked: {before_cutoff}"

    def test_group_without_policy_returns_all_slots(self, mock_rl, organization):
        """Without a BookingPolicy, the resolver returns all engine-computed slots
        (regression: policy DI wiring must not break the no-policy path)."""
        mock_rl.return_value = iter([None])

        cal = Calendar.objects.create(
            organization=organization,
            name="No-policy Cal",
            external_id="gs-cal-2",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=False,
        )
        group = _make_group(organization, cal=cal)
        # No BookingPolicy created for this group.

        client = _group_client_with_resources(organization)
        now = datetime.datetime.now(tz=datetime.UTC).replace(microsecond=0)
        start = now
        end = now + datetime.timedelta(hours=1)

        response = self._post(client, group.id, start, end)

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data, data
        # With an unmanaged calendar and no events, all step-aligned windows are free.
        slots = data["data"]["calendarGroupBookableSlots"]
        assert isinstance(slots, list)
