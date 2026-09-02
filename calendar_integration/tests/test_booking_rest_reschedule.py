"""Integration tests for ``POST /public/booking/events/reschedule/`` and
``POST /public/booking/group-events/reschedule/``.

Ports the scenarios in ``public_api/tests/test_reschedule_with_code.py`` and
``public_api/tests/test_reschedule_group_with_code.py`` (the GraphQL
``rescheduleCalendarEventWithCode`` / ``rescheduleCalendarGroupEventWithCode``
equivalents) to the REST surface, plus the byte-identical-preserved-details,
cross-event, and pinned-duration cases the plan's Phase 4 body calls for.

All requests are unauthenticated (no session/JWT). The booking code -- carried
in the ``X-Booking-Code`` header -- provides the org scope, event scope, and
RESCHEDULE permission.
"""

import datetime

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.booking_auth import BOOKING_CODE_HEADER
from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarManagementToken,
    EventAttendance,
    EventExternalAttendance,
    EventManagementPermissions,
    ExternalAttendee,
    ResourceAllocation,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import Organization, OrganizationMembership
from users.factories import UserFactory


RESCHEDULE_URL_NAME = "calendar_booking_api:booking-events-reschedule-list"
GROUP_RESCHEDULE_URL_NAME = "calendar_booking_api:booking-group-events-reschedule-list"

ORIGINAL_START = datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC)
ORIGINAL_END = datetime.datetime(2030, 6, 1, 11, 0, tzinfo=datetime.UTC)
NEW_START = datetime.datetime(2030, 6, 1, 14, 0, tzinfo=datetime.UTC)
NEW_END = datetime.datetime(2030, 6, 1, 15, 0, tzinfo=datetime.UTC)
OOW_START = datetime.datetime(2030, 6, 1, 22, 0, tzinfo=datetime.UTC)
OOW_END = datetime.datetime(2030, 6, 1, 23, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    return baker.make(Organization, name="REST Reschedule-With-Code Test Org")


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def anon_client():
    """APIClient with no Authorization header."""
    return APIClient()


def _reschedule_payload(**overrides) -> dict:
    base = {
        "start_time": NEW_START.isoformat(),
        "end_time": NEW_END.isoformat(),
        "timezone": "UTC",
    }
    base.update(overrides)
    return base


def _post(client: APIClient, url_name: str, code: str | None, payload: dict):
    headers = {BOOKING_CODE_HEADER: code} if code is not None else None
    return client.post(reverse(url_name), payload, format="json", headers=headers)


# ---------------------------------------------------------------------------
# Fixtures -- single-calendar path
# ---------------------------------------------------------------------------


@pytest.fixture
def calendar(organization):
    """A RESTRICTED calendar with managed availability windows."""
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
def calendar_group(organization):
    return baker.make(CalendarGroup, organization=organization, name="Cross-Scope Test Group")


@pytest.fixture
def available_window(organization, calendar):
    """Availability window covering both the original and the new slots."""
    return baker.make(
        AvailableTime,
        organization=organization,
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
        timezone="UTC",
    )


@pytest.fixture
def attendee_membership(organization):
    user = UserFactory().create_user(email="attendee@example.com")
    OrganizationMembership.objects.get_or_create(user=user, organization=organization)
    return user


@pytest.fixture
def existing_event(organization, calendar, attendee_membership):
    """An existing event with a title, description, internal + external attendee."""
    event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        title="Original Title",
        description="Original description.",
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0),
        external_id="",
    )
    EventAttendance.objects.create(
        organization=organization,
        event=event,
        membership_user_id=attendee_membership.id,
    )
    external_attendee = baker.make(
        ExternalAttendee,
        organization=organization,
        email="patient@example.com",
        name="Pat Patient",
    )
    baker.make(
        EventExternalAttendance,
        organization=organization,
        event=event,
        external_attendee=external_attendee,
    )
    return event


@pytest.fixture
def resource_calendar(organization):
    """A calendar allocated to ``existing_event`` as a resource allocation.

    Deliberately NOT ``calendar_type=CalendarType.RESOURCE``: with that type
    set, ``serialize_event_data_input_util``'s resource-comprehension
    (``calendar_integration/services/calendar_service_utils.py``) accesses
    a non-existent ``.calendar`` attribute on the ``Calendar`` rows it
    iterates and raises ``AttributeError`` -- a pre-existing bug unrelated
    to this phase's reschedule endpoints (out of scope here; also latent in
    the GraphQL original and the existing
    ``test_update_event_with_resource_allocations`` unit test, which sidesteps
    it the same way by never setting ``calendar_type=RESOURCE`` on its
    resource calendars).
    """
    return baker.make(
        Calendar,
        organization=organization,
        name="Resource Room",
        external_id="resource-room-reschedule-rest-test",
        provider=CalendarProvider.INTERNAL,
    )


@pytest.fixture
def existing_event_with_resource(existing_event, organization, resource_calendar):
    """``existing_event`` plus a ResourceAllocation, for the preserved-details test."""
    ResourceAllocation.objects.create(
        organization=organization,
        event_fk=existing_event,
        calendar_fk=resource_calendar,
    )
    return existing_event


@pytest.fixture
def reschedule_code(permission_service, organization, calendar, existing_event):
    """A valid single-use RESCHEDULE code bound to ``existing_event``."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_id=calendar.id,
        event_id=existing_event.id,
    )
    return token, code


@pytest.fixture
def create_code(permission_service, organization, calendar):
    """A CREATE-only code -- wrong permission for rescheduling."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )
    return token, code


@pytest.fixture
def group_reschedule_code(permission_service, organization, calendar_group, existing_event):
    """A group-scoped RESCHEDULE code -- wrong scope for the single-calendar endpoint."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_group_id=calendar_group.id,
        event_id=existing_event.id,
    )
    return token, code


@pytest.fixture
def other_event(organization, calendar):
    """A second event on the SAME calendar, not bound to any reschedule code."""
    return baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        title="Other Event",
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 2, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 2, 11, 0),
        external_id="other-event-reschedule-001",
    )


# ---------------------------------------------------------------------------
# Scenario 1: Happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRescheduleCalendarEventWithCodeHappyPath:
    def test_happy_path_reschedules_event_and_consumes_code(
        self,
        anon_client,
        reschedule_code,
        organization,
        existing_event,
        available_window,  # noqa: ARG002
    ):
        token, code = reschedule_code

        response = _post(anon_client, RESCHEDULE_URL_NAME, code, _reschedule_payload())

        assert response.status_code == status.HTTP_201_CREATED, response.content
        body = response.json()
        assert body["id"] == existing_event.id

        token.refresh_from_db()
        assert token.used_at is not None
        assert token.consumed_source_ip is not None

        existing_event.refresh_from_db()
        assert existing_event.start_time_tz_unaware.replace(tzinfo=None) == NEW_START.replace(
            tzinfo=None
        )
        assert existing_event.end_time_tz_unaware.replace(tzinfo=None) == NEW_END.replace(
            tzinfo=None
        )


# ---------------------------------------------------------------------------
# Preserved details: byte-identical after reschedule
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRescheduleCalendarEventWithCodePreservedDetails:
    def test_title_description_attendees_and_resource_allocations_preserved(
        self,
        anon_client,
        permission_service,
        organization,
        calendar,
        existing_event_with_resource,
        attendee_membership,
        resource_calendar,
        available_window,  # noqa: ARG002
    ):
        """Only the time fields change -- everything else survives byte-identical.

        This is what makes ``_determine_required_update_permissions`` yield
        exactly ``{RESCHEDULE}``: if the snapshot were rebuilt instead of
        copied, an event with attendees would demand UPDATE_ATTENDEES too, and
        a RESCHEDULE-only code would then fail with a misleading NOT_PERMITTED.
        """
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_id=calendar.id,
            event_id=existing_event_with_resource.id,
        )

        response = _post(anon_client, RESCHEDULE_URL_NAME, code, _reschedule_payload())

        assert response.status_code == status.HTTP_201_CREATED, response.content

        existing_event_with_resource.refresh_from_db()
        assert existing_event_with_resource.title == "Original Title"
        assert existing_event_with_resource.description == "Original description."

        attendances = list(
            EventAttendance.objects.filter_by_organization(organization.id).filter(
                event=existing_event_with_resource
            )
        )
        assert len(attendances) == 1
        assert attendances[0].membership_user_id == attendee_membership.id

        external_attendances = list(
            EventExternalAttendance.objects.filter_by_organization(organization.id)
            .select_related("external_attendee")
            .filter(event=existing_event_with_resource)
        )
        assert len(external_attendances) == 1
        assert external_attendances[0].external_attendee.email == "patient@example.com"
        assert external_attendances[0].external_attendee.name == "Pat Patient"

        resource_allocations = list(
            ResourceAllocation.objects.filter_by_organization(organization.id).filter(
                event=existing_event_with_resource
            )
        )
        assert len(resource_allocations) == 1
        assert resource_allocations[0].calendar_fk_id == resource_calendar.id

        # Times, and only times, changed.
        assert existing_event_with_resource.start_time_tz_unaware.replace(
            tzinfo=None
        ) == NEW_START.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Scenario: Slot outside availability does not consume
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRescheduleCalendarEventWithCodeSlotUnavailable:
    def test_slot_outside_window_does_not_consume(
        self,
        anon_client,
        reschedule_code,
        existing_event,
        available_window,  # noqa: ARG002 -- window is 09:00-17:00 UTC
    ):
        token, code = reschedule_code

        response = _post(
            anon_client,
            RESCHEDULE_URL_NAME,
            code,
            _reschedule_payload(
                start_time=OOW_START.isoformat(),
                end_time=OOW_END.isoformat(),
            ),
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error_code"] == "SLOT_UNAVAILABLE"

        token.refresh_from_db()
        assert token.used_at is None

        existing_event.refresh_from_db()
        assert existing_event.start_time_tz_unaware.replace(tzinfo=None) == ORIGINAL_START.replace(
            tzinfo=None
        )

    def test_after_failed_reschedule_code_can_still_be_used(
        self,
        anon_client,
        reschedule_code,
        available_window,  # noqa: ARG002
    ):
        token, code = reschedule_code

        fail_response = _post(
            anon_client,
            RESCHEDULE_URL_NAME,
            code,
            _reschedule_payload(start_time=OOW_START.isoformat(), end_time=OOW_END.isoformat()),
        )
        assert fail_response.status_code == status.HTTP_409_CONFLICT
        token.refresh_from_db()
        assert token.used_at is None

        success_response = _post(anon_client, RESCHEDULE_URL_NAME, code, _reschedule_payload())
        assert success_response.status_code == status.HTTP_201_CREATED, success_response.content
        token.refresh_from_db()
        assert token.used_at is not None


# ---------------------------------------------------------------------------
# Scenario: Wrong permission
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRescheduleCalendarEventWithCodeWrongPermission:
    def test_create_only_code_returns_not_permitted(
        self,
        anon_client,
        create_code,
        existing_event,
    ):
        _token, code = create_code

        response = _post(anon_client, RESCHEDULE_URL_NAME, code, _reschedule_payload())

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "NOT_PERMITTED"

        existing_event.refresh_from_db()
        assert existing_event.start_time_tz_unaware.replace(tzinfo=None) == ORIGINAL_START.replace(
            tzinfo=None
        )


# ---------------------------------------------------------------------------
# Cross-routing: group code on the single-calendar endpoint, and vice versa
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRescheduleCalendarEventWithCodeCrossRouting:
    def test_group_code_on_single_endpoint_returns_not_permitted(
        self,
        anon_client,
        group_reschedule_code,
        existing_event,
    ):
        _token, code = group_reschedule_code

        response = _post(anon_client, RESCHEDULE_URL_NAME, code, _reschedule_payload())

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "NOT_PERMITTED"
        assert "group" in response.json()["detail"].lower()

        existing_event.refresh_from_db()
        assert existing_event.start_time_tz_unaware.replace(tzinfo=None) == ORIGINAL_START.replace(
            tzinfo=None
        )


# ---------------------------------------------------------------------------
# Event binding: a code minted for event A cannot touch event B
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRescheduleCalendarEventWithCodeEventBinding:
    def test_code_only_affects_its_own_event(
        self,
        anon_client,
        reschedule_code,
        existing_event,
        other_event,
        available_window,  # noqa: ARG002
    ):
        """The code is bound to ``existing_event``; ``other_event`` (same calendar)
        is untouched, and there is no client-supplied event_id to redirect it."""
        _token, code = reschedule_code

        response = _post(anon_client, RESCHEDULE_URL_NAME, code, _reschedule_payload())

        assert response.status_code == status.HTTP_201_CREATED, response.content
        assert response.json()["id"] == existing_event.id

        other_event.refresh_from_db()
        assert other_event.start_time_tz_unaware.replace(tzinfo=None) == datetime.datetime(
            2030, 6, 2, 10, 0
        )


# ---------------------------------------------------------------------------
# Lifecycle rejections
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRescheduleCalendarEventWithCodeLifecycleRejections:
    def test_expired_code_returns_expired(
        self, anon_client, permission_service, organization, calendar, existing_event
    ):
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_id=calendar.id,
            event_id=existing_event.id,
            expires_at=past,
        )

        response = _post(anon_client, RESCHEDULE_URL_NAME, code, _reschedule_payload())

        assert response.status_code == status.HTTP_410_GONE
        assert response.json()["error_code"] == "EXPIRED"

    def test_revoked_code_returns_revoked(
        self, anon_client, permission_service, organization, calendar, existing_event
    ):
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_id=calendar.id,
            event_id=existing_event.id,
        )
        permission_service.revoke_token(organization_id=organization.id, token_id=token.id)

        response = _post(anon_client, RESCHEDULE_URL_NAME, code, _reschedule_payload())

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "REVOKED"

    def test_invalid_code_returns_invalid_code(self, anon_client):
        response = _post(
            anon_client, RESCHEDULE_URL_NAME, "aW52YWxpZHJlc2NoZWR1bGVjb2Rl", _reschedule_payload()
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error_code"] == "INVALID_CODE"

    def test_used_code_returns_already_used(
        self, anon_client, permission_service, organization, calendar, existing_event
    ):
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_id=calendar.id,
            event_id=existing_event.id,
        )
        CalendarManagementToken.original_manager.filter(id=token.id).update(
            used_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        )

        response = _post(anon_client, RESCHEDULE_URL_NAME, code, _reschedule_payload())

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error_code"] == "ALREADY_USED"


# ---------------------------------------------------------------------------
# Pinned duration: constrains the NEW span, not the event's current one
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRescheduleCalendarEventWithCodePinnedDuration:
    def test_pin_refuses_wrong_span_even_when_event_currently_matches_it(
        self,
        anon_client,
        permission_service,
        organization,
        calendar,
        attendee_membership,  # noqa: ARG002
        available_window,  # noqa: ARG002
    ):
        """A 30-minute-pinned code refuses a move to a 45-minute span, EVEN
        THOUGH the event being moved is currently 45 minutes long -- the pin
        constrains the target span, not the event's present one. The refusal
        must not consume the code."""
        event = baker.make(
            CalendarEvent,
            organization=organization,
            calendar=calendar,
            title="Currently 45 Minutes",
            timezone="UTC",
            start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
            end_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 45),
            external_id="",
        )
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_id=calendar.id,
            event_id=event.id,
            duration=datetime.timedelta(minutes=30),
        )

        # Move to a NEW 45-minute span -- same duration as the event's CURRENT
        # span, but not the code's pinned 30 minutes.
        payload = _reschedule_payload(
            start_time=NEW_START.isoformat(),
            end_time=(NEW_START + datetime.timedelta(minutes=45)).isoformat(),
        )

        response = _post(anon_client, RESCHEDULE_URL_NAME, code, payload)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        body = response.json()
        assert body["error_code"] == "NOT_PERMITTED"
        assert "30 minute" in body["detail"]

        token.refresh_from_db()
        assert token.used_at is None

        event.refresh_from_db()
        assert event.start_time_tz_unaware.replace(tzinfo=None) == datetime.datetime(
            2030, 6, 1, 10, 0
        )

    def test_pin_accepts_exact_span_move(
        self,
        anon_client,
        permission_service,
        organization,
        calendar,
        existing_event,
        available_window,  # noqa: ARG002
    ):
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_id=calendar.id,
            event_id=existing_event.id,
            duration=datetime.timedelta(minutes=30),
        )
        payload = _reschedule_payload(
            end_time=(NEW_START + datetime.timedelta(minutes=30)).isoformat()
        )

        response = _post(anon_client, RESCHEDULE_URL_NAME, code, payload)

        assert response.status_code == status.HTTP_201_CREATED, response.content
        token.refresh_from_db()
        assert token.used_at is not None


# ---------------------------------------------------------------------------
# Group reschedule
# ---------------------------------------------------------------------------


@pytest.fixture
def primary_calendar(organization):
    return baker.make(
        Calendar,
        organization=organization,
        name="Primary Calendar",
        external_id="primary-cal-reschedulegrp-rest-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def secondary_calendar(organization):
    return baker.make(
        Calendar,
        organization=organization,
        name="Room Calendar",
        external_id="room-cal-reschedulegrp-rest-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.RESOURCE,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def group(organization, primary_calendar, secondary_calendar):
    grp = baker.make(CalendarGroup, organization=organization, name="Test Group")
    slot_a = CalendarGroupSlot.objects.create(
        organization=organization, group=grp, name="Physicians", order=0, required_count=1
    )
    slot_b = CalendarGroupSlot.objects.create(
        organization=organization, group=grp, name="Rooms", order=1, required_count=1
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot_a, calendar=primary_calendar
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot_b, calendar=secondary_calendar
    )
    return grp


@pytest.fixture
def group_availability_windows(organization, primary_calendar, secondary_calendar):
    windows = []
    for cal in (primary_calendar, secondary_calendar):
        windows.append(
            AvailableTime.objects.create(
                organization=organization,
                calendar=cal,
                start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
                end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
                timezone="UTC",
            )
        )
    return windows


@pytest.fixture
def grouped_event(organization, group, primary_calendar, secondary_calendar):
    event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=primary_calendar,
        calendar_group=group,
        title="Original Group Title",
        description="Original group description.",
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0),
        external_id="",
    )
    BlockedTime.objects.create(
        organization=organization,
        calendar=secondary_calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0),
        timezone="UTC",
        reason=f"Group booking: {event.title}",
        external_id=f"group-event-{event.id}-cal-{secondary_calendar.id}",
    )
    external_attendee = baker.make(
        ExternalAttendee, organization=organization, email="patient@example.com", name="Pat Patient"
    )
    baker.make(
        EventExternalAttendance,
        organization=organization,
        event=event,
        external_attendee=external_attendee,
    )
    return event


@pytest.fixture
def group_reschedule_own_code(permission_service, organization, group, grouped_event):
    """A valid single-use GROUP RESCHEDULE code bound to ``grouped_event``."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_group_id=group.id,
        event_id=grouped_event.id,
    )
    return token, code


@pytest.fixture
def calendar_scoped_reschedule_code(
    permission_service, organization, primary_calendar, grouped_event
):
    """A RESCHEDULE code scoped to a single calendar only -- wrong scope for the group endpoint."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_id=primary_calendar.id,
        event_id=grouped_event.id,
    )
    return token, code


@pytest.mark.django_db
class TestRescheduleCalendarGroupEventWithCodeHappyPath:
    def test_happy_path_reschedules_grouped_event_and_updates_blocked_times(
        self,
        anon_client,
        group_reschedule_own_code,
        organization,
        secondary_calendar,
        grouped_event,
        group_availability_windows,  # noqa: ARG002
    ):
        token, code = group_reschedule_own_code

        response = _post(anon_client, GROUP_RESCHEDULE_URL_NAME, code, _reschedule_payload())

        assert response.status_code == status.HTTP_201_CREATED, response.content
        body = response.json()
        assert body["id"] == grouped_event.id

        token.refresh_from_db()
        assert token.used_at is not None

        grouped_event.refresh_from_db()
        assert grouped_event.start_time_tz_unaware.replace(tzinfo=None) == NEW_START.replace(
            tzinfo=None
        )
        assert grouped_event.calendar_group_fk_id == token.calendar_group_fk_id

        # Non-primary BlockedTime moved with it.
        blocked_time = BlockedTime.objects.filter_by_organization(organization.id).get(
            external_id=f"group-event-{grouped_event.id}-cal-{secondary_calendar.id}"
        )
        assert blocked_time.start_time_tz_unaware.replace(tzinfo=None) == NEW_START.replace(
            tzinfo=None
        )

    def test_title_description_and_attendees_preserved(
        self,
        anon_client,
        group_reschedule_own_code,
        organization,
        grouped_event,
        group_availability_windows,  # noqa: ARG002
    ):
        _token, code = group_reschedule_own_code

        response = _post(anon_client, GROUP_RESCHEDULE_URL_NAME, code, _reschedule_payload())

        assert response.status_code == status.HTTP_201_CREATED, response.content

        grouped_event.refresh_from_db()
        assert grouped_event.title == "Original Group Title"
        assert grouped_event.description == "Original group description."

        external_attendances = list(
            EventExternalAttendance.objects.filter_by_organization(organization.id)
            .select_related("external_attendee")
            .filter(event=grouped_event)
        )
        assert len(external_attendances) == 1
        assert external_attendances[0].external_attendee.email == "patient@example.com"


@pytest.mark.django_db
class TestRescheduleCalendarGroupEventWithCodeCrossRouting:
    def test_calendar_scoped_code_on_group_endpoint_returns_not_permitted(
        self,
        anon_client,
        calendar_scoped_reschedule_code,
        grouped_event,
    ):
        _token, code = calendar_scoped_reschedule_code

        response = _post(anon_client, GROUP_RESCHEDULE_URL_NAME, code, _reschedule_payload())

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "NOT_PERMITTED"
        assert "single-calendar" in response.json()["detail"].lower()

        grouped_event.refresh_from_db()
        assert grouped_event.start_time_tz_unaware.replace(tzinfo=None) == ORIGINAL_START.replace(
            tzinfo=None
        )


@pytest.mark.django_db
class TestRescheduleCalendarGroupEventWithCodeSlotUnavailable:
    def test_slot_outside_window_does_not_consume(
        self,
        anon_client,
        group_reschedule_own_code,
        grouped_event,
        group_availability_windows,  # noqa: ARG002 -- windows are 09:00-17:00 UTC
    ):
        token, code = group_reschedule_own_code

        response = _post(
            anon_client,
            GROUP_RESCHEDULE_URL_NAME,
            code,
            _reschedule_payload(start_time=OOW_START.isoformat(), end_time=OOW_END.isoformat()),
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error_code"] == "SLOT_UNAVAILABLE"

        token.refresh_from_db()
        assert token.used_at is None


@pytest.mark.django_db
class TestRescheduleCalendarGroupEventWithCodeLifecycleRejections:
    def test_expired_code_returns_expired(
        self, anon_client, permission_service, organization, group, grouped_event
    ):
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_group_id=group.id,
            event_id=grouped_event.id,
            expires_at=past,
        )

        response = _post(anon_client, GROUP_RESCHEDULE_URL_NAME, code, _reschedule_payload())

        assert response.status_code == status.HTTP_410_GONE
        assert response.json()["error_code"] == "EXPIRED"
