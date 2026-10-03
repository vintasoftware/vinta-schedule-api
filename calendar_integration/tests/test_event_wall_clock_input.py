"""Event write surfaces read naive ``start_time`` / ``end_time`` as wall-clock in ``timezone``.

The frontend contract is: send naive local date-times plus the IANA timezone as a
separate field. Availability windows are already stored that way (the naive value is
the wall-clock), so an event entry point that instead reads the naive value as UTC
puts the event ``UTC offset`` hours away from the window it was booked in -- and on a
calendar that manages its availability windows, the booking is rejected as "not
available" even though the slot is free.

Each test seeds a recurring 09:00-17:00 ``America/Sao_Paulo`` window, books
10:00-11:00 on a later day through one surface, and checks the event lands on
13:00Z (10:00 local).
"""

import datetime
import zoneinfo
from unittest.mock import patch

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient, APIRequestFactory

from calendar_integration.booking_auth import BOOKING_CODE_HEADER
from calendar_integration.constants import CalendarProvider, EventManagementPermissions
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    EventRecurrenceException,
    RecurrenceRule,
)
from calendar_integration.serializers import CalendarEventSerializer
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.calendar_service_utils import wall_clock_to_utc
from calendar_integration.services.dataclasses import CalendarEventAdapterOutputData
from calendar_integration.tests.test_event_creation_surfaces import (
    _CREATE_APPOINTMENT_TYPE_EVENT_WITH_CODE,
    _CREATE_EVENT_WITH_CODE,
    _SCHEDULE_EVENT,
    _appointment_type_booking_code,
    _appointment_type_with_one_slot,
    _google_backed_owner,
    _scoped_system_user,
    mock_google_adapter,  # noqa: F401 -- a fixture, used by name
)
from organizations.models import Organization, OrganizationMembership
from users.models import Profile, User


TZ = "America/Sao_Paulo"
START = "2030-06-05T10:00:00"
END = "2030-06-05T11:00:00"
EXPECTED_START = datetime.datetime(2030, 6, 5, 13, 0, tzinfo=datetime.UTC)
EXPECTED_END = datetime.datetime(2030, 6, 5, 14, 0, tzinfo=datetime.UTC)

_RESCHEDULE_WITH_CODE = """
mutation RescheduleCalendarEventWithCode($input: RescheduleWithCodeInput!) {
    rescheduleCalendarEventWithCode(input: $input) {
        success
        errorCode
        errorMessage
        event { id }
    }
}
"""


def _seed_window(calendar: Calendar, tz: str = TZ) -> None:
    calendar.manage_available_windows = True
    calendar.save(update_fields=["manage_available_windows"])
    service = CalendarService()
    service.initialize_without_provider(organization=calendar.organization)
    service.create_available_time(
        calendar=calendar,
        start_time=datetime.datetime(2030, 6, 3, 9, 0),
        end_time=datetime.datetime(2030, 6, 3, 17, 0),
        timezone=tz,
        rrule_string="FREQ=DAILY",
    )


def _internal_calendar(organization: Organization, tz: str = TZ) -> Calendar:
    calendar = baker.make(
        Calendar,
        organization=organization,
        provider=CalendarProvider.INTERNAL,
        accepts_public_scheduling=False,
        external_id=f"wall-clock-cal-{organization.pk}",
    )
    _seed_window(calendar, tz)
    return calendar


def _assert_single_event_at_expected_instant(calendar: Calendar) -> None:
    event = CalendarEvent.original_manager.get(calendar_fk=calendar)
    assert (event.start_time, event.end_time) == (EXPECTED_START, EXPECTED_END)
    assert event.timezone == TZ


def _graphql(query: str, variables: dict, headers: dict | None = None) -> dict:
    response = APIClient().post(
        "/graphql/",
        data={"query": query, "variables": variables},
        format="json",
        headers=headers,
    )
    assert response.status_code == 200, response.content
    return response.json()


def test_wall_clock_to_utc_reads_naive_value_in_timezone():
    assert wall_clock_to_utc(datetime.datetime(2030, 6, 5, 10, 0), TZ) == EXPECTED_START


def test_wall_clock_to_utc_reads_drf_coerced_value_as_wall_clock():
    # DRF turns naive input into the same digits tagged UTC; those digits are the wall-clock.
    drf_value = datetime.datetime(2030, 6, 5, 10, 0, tzinfo=datetime.UTC)
    assert wall_clock_to_utc(drf_value, TZ) == EXPECTED_START


def test_wall_clock_to_utc_rejects_unknown_timezone():
    with pytest.raises(ValueError, match="Invalid IANA timezone"):
        wall_clock_to_utc(datetime.datetime(2030, 6, 5, 10, 0), "Not/AZone")


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
def test_public_schedule_event(mock_rate_limiter):
    mock_rate_limiter.return_value = iter([None])
    organization = baker.make(Organization)
    owner = User.objects.create_user(email=f"owner-{organization.pk}@example.com", password="x")
    Profile.objects.create(user=owner)
    membership = OrganizationMembership.objects.create(
        user=owner, organization=organization, is_active=True
    )
    calendar = _internal_calendar(organization)
    CalendarOwnership.objects.create(
        calendar=calendar, membership_user_id=owner.id, organization=organization
    )
    system_user, token = _scoped_system_user(organization, membership)

    data = _graphql(
        _SCHEDULE_EVENT,
        {
            "input": {
                "organizationId": organization.id,
                "calendarId": calendar.id,
                "title": "Wall clock",
                "startTime": START,
                "endTime": END,
                "timezone": TZ,
            }
        },
        headers={"authorization": f"Bearer {system_user.id}:{token}"},
    )

    assert not data.get("errors"), data
    _assert_single_event_at_expected_instant(calendar)


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
def test_create_calendar_event_with_code(mock_rate_limiter):
    mock_rate_limiter.return_value = iter([None])
    organization = baker.make(Organization)
    calendar = _internal_calendar(organization)
    _token, code = CalendarPermissionService().create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )

    data = _graphql(
        _CREATE_EVENT_WITH_CODE,
        {
            "input": {
                "code": code,
                "title": "Wall clock",
                "description": "",
                "startTime": START,
                "endTime": END,
                "timezone": TZ,
                "externalAttendee": {"email": "patient@example.com", "name": "Pat"},
            }
        },
    )

    assert data["data"]["createCalendarEventWithCode"]["success"] is True, data
    _assert_single_event_at_expected_instant(calendar)


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
def test_create_appointment_type_event_with_code(mock_rate_limiter):
    mock_rate_limiter.return_value = iter([None])
    organization = baker.make(Organization)
    appointment_type, slot, calendar = _appointment_type_with_one_slot(organization)
    _seed_window(calendar)
    _token, code = _appointment_type_booking_code(organization, appointment_type)

    data = _graphql(
        _CREATE_APPOINTMENT_TYPE_EVENT_WITH_CODE,
        {
            "input": {
                "code": code,
                "title": "Wall clock",
                "description": "",
                "startTime": START,
                "endTime": END,
                "timezone": TZ,
                "slotSelections": [{"slotId": slot.id, "calendarIds": [calendar.id]}],
                "externalAttendee": {"email": "patient@example.com", "name": "Pat"},
            }
        },
    )

    assert data["data"]["createAppointmentTypeEventWithCode"]["success"] is True, data
    _assert_single_event_at_expected_instant(calendar)


@pytest.mark.django_db
def test_rest_booking_calendar_event():
    organization = baker.make(Organization)
    calendar = _internal_calendar(organization)
    _token, code = CalendarPermissionService().create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )

    response = APIClient().post(
        reverse("calendar_booking_api:booking-calendar-events-list"),
        {
            "title": "Wall clock",
            "start_time": START,
            "end_time": END,
            "timezone": TZ,
            "external_attendee": {"email": "patient@example.com", "name": "Pat"},
        },
        format="json",
        headers={BOOKING_CODE_HEADER: code},
    )

    assert response.status_code == status.HTTP_201_CREATED, response.content
    _assert_single_event_at_expected_instant(calendar)


@pytest.mark.django_db
def test_rest_booking_appointment_type_event():
    organization = baker.make(Organization)
    appointment_type, slot, calendar = _appointment_type_with_one_slot(organization)
    _seed_window(calendar)
    _token, code = _appointment_type_booking_code(organization, appointment_type)

    response = APIClient().post(
        reverse(
            "calendar_booking_api:booking-appointment-type-events-list",
            kwargs={"public_slug": appointment_type.public_booking_slug},
        ),
        {
            "title": "Wall clock",
            "start_time": START,
            "end_time": END,
            "timezone": TZ,
            "slot_selections": [{"slot_id": slot.id, "calendar_ids": [calendar.id]}],
            "external_attendee": {"email": "patient@example.com", "name": "Pat"},
        },
        format="json",
        headers={BOOKING_CODE_HEADER: code},
    )

    assert response.status_code == status.HTTP_201_CREATED, response.content
    _assert_single_event_at_expected_instant(calendar)


def _existing_event_and_reschedule_code(organization: Organization) -> tuple[Calendar, str]:
    calendar = _internal_calendar(organization)
    event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        title="Existing",
        timezone=TZ,
        start_time_tz_unaware=datetime.datetime(2030, 6, 4, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 4, 11, 0),
        external_id=f"existing-{organization.pk}",
    )
    _token, code = CalendarPermissionService().create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_id=calendar.id,
        event_id=event.id,
    )
    return calendar, code


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
def test_reschedule_calendar_event_with_code(mock_rate_limiter):
    mock_rate_limiter.return_value = iter([None])
    organization = baker.make(Organization)
    calendar, code = _existing_event_and_reschedule_code(organization)

    data = _graphql(
        _RESCHEDULE_WITH_CODE,
        {"input": {"code": code, "startTime": START, "endTime": END, "timezone": TZ}},
    )

    assert data["data"]["rescheduleCalendarEventWithCode"]["success"] is True, data
    _assert_single_event_at_expected_instant(calendar)


@pytest.mark.django_db
def test_rest_booking_reschedule():
    organization = baker.make(Organization)
    calendar, code = _existing_event_and_reschedule_code(organization)

    response = APIClient().post(
        reverse("calendar_booking_api:booking-events-reschedule-list"),
        {"start_time": START, "end_time": END, "timezone": TZ},
        format="json",
        headers={BOOKING_CODE_HEADER: code},
    )

    assert response.status_code == status.HTTP_201_CREATED, response.content
    _assert_single_event_at_expected_instant(calendar)


@pytest.mark.django_db
def test_rest_booking_rejects_end_before_start_with_naive_input():
    organization = baker.make(Organization)
    calendar = _internal_calendar(organization)
    _token, code = CalendarPermissionService().create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )

    response = APIClient().post(
        reverse("calendar_booking_api:booking-calendar-events-list"),
        {
            "title": "Wall clock",
            "start_time": END,
            "end_time": START,
            "timezone": TZ,
            "external_attendee": {"email": "patient@example.com", "name": "Pat"},
        },
        format="json",
        headers={BOOKING_CODE_HEADER: code},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.content
    assert "end_time" in response.json()


@pytest.mark.django_db
def test_rest_booking_calendar_event_in_a_positive_offset_zone():
    organization = baker.make(Organization)
    calendar = _internal_calendar(organization, "Asia/Tokyo")
    _token, code = CalendarPermissionService().create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )

    response = APIClient().post(
        reverse("calendar_booking_api:booking-calendar-events-list"),
        {
            "title": "Wall clock",
            "start_time": START,
            "end_time": END,
            "timezone": "Asia/Tokyo",
            "external_attendee": {"email": "patient@example.com", "name": "Pat"},
        },
        format="json",
        headers={BOOKING_CODE_HEADER: code},
    )

    # 10:00 in Tokyo (UTC+9) is 01:00Z.
    assert response.status_code == status.HTTP_201_CREATED, response.content
    event = CalendarEvent.original_manager.get(calendar_fk=calendar)
    assert event.start_time == datetime.datetime(2030, 6, 5, 1, 0, tzinfo=datetime.UTC)
    assert event.end_time == datetime.datetime(2030, 6, 5, 2, 0, tzinfo=datetime.UTC)


@pytest.mark.django_db
def test_rest_booking_rejects_an_unknown_timezone():
    organization = baker.make(Organization)
    calendar = _internal_calendar(organization)
    _token, code = CalendarPermissionService().create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )

    response = APIClient().post(
        reverse("calendar_booking_api:booking-calendar-events-list"),
        {
            "title": "Wall clock",
            "start_time": START,
            "end_time": END,
            "timezone": "Not/AZone",
            "external_attendee": {"email": "patient@example.com", "name": "Pat"},
        },
        format="json",
        headers={BOOKING_CODE_HEADER: code},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.content
    assert "timezone" in response.json()
    assert not CalendarEvent.original_manager.filter(calendar_fk=calendar).exists()


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
def test_create_calendar_event_with_code_rejects_an_unknown_timezone(mock_rate_limiter):
    mock_rate_limiter.return_value = iter([None])
    organization = baker.make(Organization)
    calendar = _internal_calendar(organization)
    _token, code = CalendarPermissionService().create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )

    data = _graphql(
        _CREATE_EVENT_WITH_CODE,
        {
            "input": {
                "code": code,
                "title": "Wall clock",
                "description": "",
                "startTime": START,
                "endTime": END,
                "timezone": "Not/AZone",
                "externalAttendee": {"email": "patient@example.com", "name": "Pat"},
            }
        },
    )

    assert data.get("errors"), data
    assert "Invalid IANA timezone" in data["errors"][0]["message"]
    assert not CalendarEvent.original_manager.filter(calendar_fk=calendar).exists()


# ---------------------------------------------------------------------------
# Recurring event exceptions (REST ``create-exception``)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_rest_recurring_exception_reads_modified_times_as_wall_clock(mock_google_adapter):  # noqa: F811
    organization = baker.make(Organization)
    calendar = baker.make(
        Calendar,
        organization=organization,
        provider=CalendarProvider.INTERNAL,
        external_id=f"exception-cal-{organization.pk}",
    )
    user, _social_account = _google_backed_owner(organization, calendar)
    mock_google_adapter.create_event.return_value = CalendarEventAdapterOutputData(
        calendar_external_id=calendar.external_id,
        external_id="exception-modified",
        title="Modified",
        description="",
        start_time=EXPECTED_START,
        end_time=EXPECTED_END,
        timezone=TZ,
        attendees=[],
        resources=[],
        original_payload={},
    )
    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY", organization=organization)
    rule.save()
    master = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        timezone=TZ,
        start_time_tz_unaware=datetime.datetime(2030, 6, 3, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 3, 11, 0),
        recurrence_rule=rule,
        external_id="exception-master",
    )

    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post(
        reverse("api:CalendarEvents-create-exception", kwargs={"pk": master.pk}),
        {
            "exception_date": "2030-06-05",
            "modified_start_time": "2030-06-05T15:00:00",
            "modified_end_time": "2030-06-05T16:00:00",
        },
        format="json",
    )

    # 15:00 in Sao Paulo (UTC-3) is 18:00Z.
    assert response.status_code == status.HTTP_201_CREATED, response.content
    modified = CalendarEvent.original_manager.get(pk=response.json()["id"])
    assert modified.timezone == TZ
    assert modified.start_time == datetime.datetime(2030, 6, 5, 18, 0, tzinfo=datetime.UTC)
    assert modified.end_time == datetime.datetime(2030, 6, 5, 19, 0, tzinfo=datetime.UTC)
    exception = EventRecurrenceException.objects.unscoped().get(parent_event=master)
    assert exception.modified_event_fk_id == modified.pk
    assert exception.exception_date == datetime.datetime(2030, 6, 5, 13, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# ``CalendarEventSerializer`` (REST create / update of a single event)
# ---------------------------------------------------------------------------


def _event_serializer(
    organization: Organization, data: dict, instance: CalendarEvent | None = None
) -> CalendarEventSerializer:
    user = User.objects.create_user(email=f"ser-{organization.pk}@example.com", password="x")
    Profile.objects.create(user=user)
    request = APIRequestFactory().post("/")
    request.user = user
    return CalendarEventSerializer(
        instance,
        data=data,
        partial=instance is not None,
        context={"request": request, "organization": organization},
    )


def _local_naive(instant: datetime.datetime, tz: str) -> str:
    return instant.astimezone(zoneinfo.ZoneInfo(tz)).replace(tzinfo=None).isoformat()


def _create_payload(calendar: Calendar, start: datetime.datetime, tz: str) -> dict:
    return {
        "calendar": calendar.pk,
        "title": "Wall clock",
        "start_time": _local_naive(start, tz),
        "end_time": _local_naive(start + datetime.timedelta(hours=1), tz),
        "timezone": tz,
        "resource_allocations": [],
        "attendances": [],
        "external_attendances": [],
    }


@pytest.mark.django_db
def test_serializer_accepts_a_start_that_is_in_the_future_only_once_converted():
    organization = baker.make(Organization)
    calendar = baker.make(Calendar, organization=organization)
    # In Sao Paulo the wall-clock digits trail UTC by 3 hours, so they read as the past
    # until they are converted. The instant is one hour ahead.
    start = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)

    serializer = _event_serializer(organization, _create_payload(calendar, start, TZ))

    assert serializer.is_valid(), serializer.errors


@pytest.mark.django_db
def test_serializer_rejects_a_start_that_is_in_the_past_once_converted():
    organization = baker.make(Organization)
    calendar = baker.make(Calendar, organization=organization)
    # In Tokyo the wall-clock digits run 9 hours ahead of UTC, so they read as the
    # future until they are converted. The instant is one hour behind.
    start = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)

    serializer = _event_serializer(organization, _create_payload(calendar, start, "Asia/Tokyo"))

    assert not serializer.is_valid()
    assert "Start time must be in the future." in serializer.errors["start_time"]


def _sao_paulo_event(organization: Organization) -> CalendarEvent:
    calendar = baker.make(Calendar, organization=organization)
    event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        timezone=TZ,
        start_time_tz_unaware=datetime.datetime(2030, 6, 5, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 5, 11, 0),
        external_id="serializer-event",
    )
    event.refresh_from_db()
    return event


@pytest.mark.django_db
def test_serializer_partial_update_rejects_an_end_before_the_existing_start():
    organization = baker.make(Organization)
    event = _sao_paulo_event(organization)

    # 09:00 in Sao Paulo is 12:00Z, an hour before the event starts at 13:00Z.
    serializer = _event_serializer(organization, {"end_time": "2030-06-05T09:00:00"}, event)

    assert not serializer.is_valid()
    assert "End time must be after start time." in serializer.errors["non_field_errors"]


@pytest.mark.django_db
def test_serializer_partial_update_compares_a_new_start_with_the_existing_end_instant():
    organization = baker.make(Organization)
    event = _sao_paulo_event(organization)

    # 10:30 local is before the existing 11:00 local end, so this is valid. Comparing it
    # with the end's wall-clock digits (11:00 as if UTC) would reject it.
    serializer = _event_serializer(organization, {"start_time": "2030-06-05T10:30:00"}, event)

    assert serializer.is_valid(), serializer.errors
