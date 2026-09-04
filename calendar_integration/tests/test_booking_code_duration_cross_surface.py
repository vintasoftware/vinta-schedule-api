"""Cross-surface proof that a CalendarGroup's pinned duration is unbypassable.

History correction: an earlier draft of this file proved a per-code
``token.duration`` pin was unbypassable across GraphQL's
``createCalendarEventWithCode`` / ``rescheduleCalendarEventWithCode`` (single
calendar), ``createCalendarGroupEventWithCode`` (group), and the legacy
``public/organizations/<id>/events/`` management-token surface (single
calendar). Duration pinning has moved off ``CalendarManagementToken`` onto
``CalendarGroup`` (see that field's help_text for why: a codeless
public-group booking presents no code, so a per-code pin can never reach it).
That has two consequences for this file:

- The single-calendar scenarios (``TestDurationPinGraphQLCreate``,
  ``TestDurationPinGraphQLReschedule``, ``TestDurationPinLegacyManagementTokenSurface``)
  no longer apply at all -- there is no ``Calendar.duration`` and single-calendar
  codes carry no duration constraint any more. DELETED, not re-pointed:
  there is nothing left to assert once the pin they exercised does not exist.
- The group scenario (``TestDurationPinGraphQLGroupCreate``) still applies,
  re-pointed at ``group.duration`` instead of ``token.duration``. It remains
  the regression test that proves ``CalendarGroupService.create_grouped_event``
  still passes ``start_time`` / ``end_time`` into
  ``can_perform_group_scheduling`` -- the only gate a group booking passes
  through (``create_event`` is called with ``group_authorized=True``,
  skipping its own ``can_perform_scheduling`` call entirely).

If enforcement had instead been placed in a REST view (a future phase's), a
client could launder a pinned group by presenting its code to GraphQL or the
legacy surface instead -- this file is the regression test that proves that
is not possible for the group-booking path.
"""

import datetime
from unittest.mock import patch

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    EventManagementPermissions,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import Organization


# ---------------------------------------------------------------------------
# GraphQL mutation strings
# ---------------------------------------------------------------------------

CREATE_GROUP_EVENT_WITH_CODE = """
mutation CreateCalendarGroupEventWithCode($input: CreateGroupEventWithCodeInput!) {
    createCalendarGroupEventWithCode(input: $input) {
        success
        errorCode
        errorMessage
        event { id }
    }
}
"""


def post_graphql(client: APIClient, query: str, variables: dict) -> dict:
    response = client.post(
        "/graphql/",
        data={"query": query, "variables": variables},
        format="json",
    )
    assert response.status_code == 200, response.content.decode()
    return response.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    return baker.make(Organization, name="Duration Pin Cross-Surface Org")


@pytest.fixture
def calendar(organization):
    """A RESTRICTED calendar -- the group-scoped code alone must authorize scheduling."""
    return baker.make(
        Calendar,
        organization=organization,
        name="Test Calendar",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def available_window(organization, calendar):
    """A wide availability window covering every slot used below."""
    return baker.make(
        AvailableTime,
        organization=organization,
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
        timezone="UTC",
    )


@pytest.fixture
def secondary_calendar(organization):
    """A second RESTRICTED calendar, the other slot member of ``group`` below."""
    return baker.make(
        Calendar,
        organization=organization,
        name="Secondary Calendar",
        external_id="secondary-cal-duration-pin-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.RESOURCE,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def secondary_available_window(organization, secondary_calendar):
    """Availability window for ``secondary_calendar``, mirroring ``available_window``."""
    return baker.make(
        AvailableTime,
        organization=organization,
        calendar=secondary_calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
        timezone="UTC",
    )


@pytest.fixture
def group(organization, calendar, secondary_calendar):
    """A CalendarGroup with two slots (``calendar``, ``secondary_calendar``),
    pinned to a 30-minute duration -- private (``accepts_public_scheduling=False``,
    the default), so the group-scoped CODE is what must authorize booking; the
    duration pin is independent of that and enforced regardless."""
    grp = baker.make(
        CalendarGroup,
        organization=organization,
        name="Duration Pin Group",
        duration=datetime.timedelta(minutes=30),
    )
    slot_a = CalendarGroupSlot.objects.create(
        organization=organization, group=grp, name="Primary", order=0, required_count=1
    )
    slot_b = CalendarGroupSlot.objects.create(
        organization=organization, group=grp, name="Secondary", order=1, required_count=1
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot_a, calendar=calendar
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot_b, calendar=secondary_calendar
    )
    return grp


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def anon_client():
    return APIClient()


# ---------------------------------------------------------------------------
# GraphQL: createCalendarGroupEventWithCode
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDurationPinGraphQLGroupCreate:
    """Regression test for the group-booking call site duration pinning depends on.

    ``CalendarGroupService.create_grouped_event`` is the ONLY gate a group
    booking passes through -- ``create_event`` is called with
    ``group_authorized=True``, which skips its own ``can_perform_scheduling``
    call for the primary calendar entirely. Swapping the ``start_time`` /
    ``end_time`` arguments at that call site would leave the rest of the
    suite green while silently unpinning every group booking.
    """

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_pinned_30_minute_group_rejects_60_minute_create(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        group,
        calendar,
        secondary_calendar,
        available_window,  # noqa: ARG002 — seeds DB rows consumed by create_event
        secondary_available_window,  # noqa: ARG002
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=group.id,
        )
        start = datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC)
        end = start + datetime.timedelta(hours=1)  # 60 minutes -- mismatched.
        slot_a = group.slots.get(name="Primary")
        slot_b = group.slots.get(name="Secondary")

        data = post_graphql(
            anon_client,
            CREATE_GROUP_EVENT_WITH_CODE,
            {
                "input": {
                    "code": code,
                    "title": "Should be rejected",
                    "description": "",
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "slotSelections": [
                        {"slotId": slot_a.id, "calendarIds": [calendar.id]},
                        {"slotId": slot_b.id, "calendarIds": [secondary_calendar.id]},
                    ],
                    "externalAttendee": {"email": "patient@example.com", "name": "Pat Patient"},
                }
            },
        )
        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "NOT_PERMITTED"
        assert result["event"] is None
        assert (
            not CalendarEvent.objects.filter_by_organization(organization.id)
            .filter(title="Should be rejected")
            .exists()
        )
        # Authorization failure, not a spent attempt -- the code stays usable.
        token.refresh_from_db()
        assert token.used_at is None

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_pinned_30_minute_group_accepts_exact_30_minute_create(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        group,
        calendar,
        secondary_calendar,
        available_window,  # noqa: ARG002
        secondary_available_window,  # noqa: ARG002
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=group.id,
        )
        start = datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC)
        end = start + datetime.timedelta(minutes=30)  # exact match.
        slot_a = group.slots.get(name="Primary")
        slot_b = group.slots.get(name="Secondary")

        data = post_graphql(
            anon_client,
            CREATE_GROUP_EVENT_WITH_CODE,
            {
                "input": {
                    "code": code,
                    "title": "Should succeed",
                    "description": "",
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "slotSelections": [
                        {"slotId": slot_a.id, "calendarIds": [calendar.id]},
                        {"slotId": slot_b.id, "calendarIds": [secondary_calendar.id]},
                    ],
                    "externalAttendee": {"email": "patient@example.com", "name": "Pat Patient"},
                }
            },
        )
        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is True, result
        assert result["event"] is not None
        token.refresh_from_db()
        assert token.used_at is not None
