"""Setting an event's video meeting through the public API.

An organization-wide token books an appointment type with ``meeting`` and changes it
later through ``updateCalendarEvent``; an owner-scoped token schedules an event with
``meeting`` through ``scheduleEvent``. Covered here, over the real ``/graphql/`` stack:

- a booking with a CUSTOM meeting saves and returns the link;
- a booking with a GENERATED meeting on a calendar with no provider is refused, since
  nothing would ever create the link;
- a meeting with a bad url is refused and creates no event;
- ``scheduleEvent`` saves a CUSTOM meeting and refuses a bad one with no event;
- an update that omits the field keeps the meeting; ``null`` removes it; a new value
  replaces it; a bad url is refused and changes nothing.

How the meeting reaches Google is covered by the adapter and service tests, and which
urls fit which kind by ``calendar_integration/tests/services/test_event_meeting.py``.
"""

import pytest
from model_bakery import baker

from calendar_integration.constants import MeetingKind
from calendar_integration.models import Calendar, CalendarEvent, CalendarOwnership
from organizations.models import Organization, OrganizationMembership
from public_api.constants import PublicAPIResources
from public_api.tests.helpers import GraphQLPost, make_token_post
from users.models import User


pytestmark = pytest.mark.django_db

CUSTOM_URL = "https://zoom.us/j/123"
NO_PROVIDER_MESSAGE = "This calendar cannot generate a meeting link. Use a custom link instead."

CREATE_APPOINTMENT_TYPE_EVENT_MUTATION = """
mutation CreateAppointmentTypeEvent($input: AppointmentTypeEventInput!) {
    createAppointmentTypeEvent(input: $input) {
        success
        errorMessage
        event { id meetingKind meetingUrl }
    }
}
"""

SCHEDULE_EVENT_MUTATION = """
mutation ScheduleEvent($input: ScheduleEventInput!) {
    scheduleEvent(input: $input) { id meetingKind meetingUrl }
}
"""

UPDATE_CALENDAR_EVENT_MUTATION = """
mutation UpdateCalendarEvent($input: UpdateCalendarEventInput!) {
    updateCalendarEvent(input: $input) { meetingKind meetingUrl }
}
"""


def _book(post, organization, appointment_type_with_slot, meeting: dict | None) -> dict:
    appointment_type, slot = appointment_type_with_slot
    calendar_id = slot.memberships.get().calendar_fk_id
    return post(
        CREATE_APPOINTMENT_TYPE_EVENT_MUTATION,
        {
            "input": {
                "organizationId": organization.id,
                "appointmentTypeId": appointment_type.id,
                "title": "Visit",
                "description": "",
                "startTime": "2030-06-01T10:00:00",
                "endTime": "2030-06-01T10:30:00",
                "timezone": "UTC",
                "slotSelections": [{"slotId": slot.id, "calendarIds": [calendar_id]}],
                "externalAttendances": [
                    {"externalAttendee": {"email": "patient@example.com", "name": "Patient"}}
                ],
                "meeting": meeting,
            }
        },
    )["data"]["createAppointmentTypeEvent"]


@pytest.fixture
def owner_scoped_post(organization: Organization, booking_calendar: Calendar) -> GraphQLPost:
    """post() for a token scoped to the member who owns ``booking_calendar``."""
    membership = baker.make(
        OrganizationMembership, user=baker.make(User), organization=organization, is_active=True
    )
    baker.make(
        CalendarOwnership,
        organization=organization,
        calendar=booking_calendar,
        membership_user_id=membership.user_id,
    )
    return make_token_post(
        organization, [PublicAPIResources.CALENDAR_EVENT], scoped_to_membership=membership
    )


def _schedule(post, organization, calendar: Calendar, meeting: dict | None) -> dict:
    return post(
        SCHEDULE_EVENT_MUTATION,
        {
            "input": {
                "organizationId": organization.id,
                "calendarId": calendar.id,
                "title": "Visit",
                "startTime": "2030-06-01T10:00:00",
                "endTime": "2030-06-01T10:30:00",
                "timezone": "UTC",
                "meeting": meeting,
            }
        },
    )


def _update(post, organization, event_id: int, **fields) -> dict:
    return post(
        UPDATE_CALENDAR_EVENT_MUTATION,
        {"input": {"organizationId": organization.id, "eventId": event_id, **fields}},
    )


def _stored_meeting(organization, event_id: int) -> tuple[str | None, str]:
    event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=event_id)
    return event.meeting_kind, event.meeting_url


def _book_custom(post, organization, appointment_type_with_slot) -> int:
    booked = _book(
        post, organization, appointment_type_with_slot, {"kind": "CUSTOM", "url": CUSTOM_URL}
    )
    return int(booked["event"]["id"])


def test_booking_with_a_custom_meeting_saves_the_link(
    org_wide_post, organization, appointment_type_with_slot
):
    result = _book(
        org_wide_post,
        organization,
        appointment_type_with_slot,
        {"kind": "CUSTOM", "url": CUSTOM_URL},
    )

    assert result["errorMessage"] is None
    assert (result["event"]["meetingKind"], result["event"]["meetingUrl"]) == (
        "CUSTOM",
        CUSTOM_URL,
    )
    assert _stored_meeting(organization, int(result["event"]["id"])) == (
        MeetingKind.CUSTOM,
        CUSTOM_URL,
    )


@pytest.mark.parametrize(
    ("meeting", "message"),
    [
        ({"kind": "CUSTOM", "url": "http://zoom.us/j/123"}, "A custom meeting needs an https url."),
        # Nothing would ever create the link on a calendar with no provider.
        ({"kind": "GENERATED"}, NO_PROVIDER_MESSAGE),
    ],
    ids=["http", "generated-without-provider"],
)
def test_booking_with_a_bad_meeting_is_refused(
    org_wide_post, organization, appointment_type_with_slot, meeting, message
):
    result = _book(org_wide_post, organization, appointment_type_with_slot, meeting)

    assert result == {"success": False, "errorMessage": message, "event": None}
    assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


def test_schedule_event_with_a_custom_meeting_saves_the_link(
    owner_scoped_post, organization, booking_calendar
):
    result = _schedule(
        owner_scoped_post, organization, booking_calendar, {"kind": "CUSTOM", "url": CUSTOM_URL}
    )

    event = result["data"]["scheduleEvent"]
    assert (event["meetingKind"], event["meetingUrl"]) == ("CUSTOM", CUSTOM_URL)
    assert _stored_meeting(organization, int(event["id"])) == (MeetingKind.CUSTOM, CUSTOM_URL)


@pytest.mark.parametrize(
    ("meeting", "message"),
    [
        ({"kind": "CUSTOM", "url": "http://zoom.us/j/123"}, "A custom meeting needs an https url."),
        ({"kind": "GENERATED"}, NO_PROVIDER_MESSAGE),
    ],
    ids=["http", "generated-without-provider"],
)
def test_schedule_event_with_a_bad_meeting_is_refused(
    owner_scoped_post, organization, booking_calendar, meeting, message
):
    result = _schedule(owner_scoped_post, organization, booking_calendar, meeting)

    assert result["data"] is None
    assert [e["message"] for e in result["errors"]] == [message]
    assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


@pytest.mark.parametrize(
    ("fields", "returned", "stored"),
    [
        (
            {"title": "Renamed"},
            {"meetingKind": "CUSTOM", "meetingUrl": CUSTOM_URL},
            (MeetingKind.CUSTOM, CUSTOM_URL),
        ),
        ({"meeting": None}, {"meetingKind": None, "meetingUrl": None}, ("", "")),
        (
            {"meeting": {"kind": "CUSTOM", "url": "https://zoom.us/j/456"}},
            {"meetingKind": "CUSTOM", "meetingUrl": "https://zoom.us/j/456"},
            (MeetingKind.CUSTOM, "https://zoom.us/j/456"),
        ),
    ],
    ids=["omitted-keeps-it", "null-removes-it", "new-value-replaces-it"],
)
def test_update_sets_the_meeting(
    org_wide_post, organization, appointment_type_with_slot, fields, returned, stored
):
    event_id = _book_custom(org_wide_post, organization, appointment_type_with_slot)

    updated = _update(org_wide_post, organization, event_id, **fields)

    assert updated == {"data": {"updateCalendarEvent": returned}}
    assert _stored_meeting(organization, event_id) == stored


@pytest.mark.parametrize(
    ("meeting", "message"),
    [
        ({"kind": "CUSTOM", "url": "http://zoom.us/j/1"}, "A custom meeting needs an https url."),
        ({"kind": "GENERATED"}, NO_PROVIDER_MESSAGE),
    ],
    ids=["http", "generated-without-provider"],
)
def test_update_with_a_bad_meeting_is_refused(
    org_wide_post, organization, appointment_type_with_slot, meeting, message
):
    event_id = _book_custom(org_wide_post, organization, appointment_type_with_slot)

    updated = _update(org_wide_post, organization, event_id, meeting=meeting)

    assert [e["message"] for e in updated["errors"]] == [message]
    assert _stored_meeting(organization, event_id) == (MeetingKind.CUSTOM, CUSTOM_URL)
