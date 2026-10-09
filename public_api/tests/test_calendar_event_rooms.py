"""Booking rooms (resource calendars) on an event through the public API.

An organization-wide token books an appointment type with ``resourceCalendarIds`` and
changes them later through ``updateCalendarEvent``. Covered here, over the real
``/graphql/`` stack:

- a booking with a room writes one resource allocation for it;
- a booking naming another organization's room, a personal calendar or a disabled
  room is refused and creates no event;
- a booking naming a room already taken at that time is refused and creates no event;
- an update that omits the field keeps the rooms, even one disabled since; an empty
  list clears them; a new list replaces them;
- an update naming another organization's room, or adding a room taken at the
  event's time, is refused and changes nothing.
"""

import datetime
import uuid

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType, CalendarVisibility
from calendar_integration.models import BlockedTime, Calendar, CalendarEvent, ResourceAllocation
from organizations.models import Organization


pytestmark = pytest.mark.django_db

CREATE_APPOINTMENT_TYPE_EVENT_MUTATION = """
mutation CreateAppointmentTypeEvent($input: AppointmentTypeEventInput!) {
    createAppointmentTypeEvent(input: $input) {
        success
        errorMessage
        event { id }
    }
}
"""

UPDATE_CALENDAR_EVENT_MUTATION = """
mutation UpdateCalendarEvent($input: UpdateCalendarEventInput!) {
    updateCalendarEvent(input: $input) { id }
}
"""


def _calendar(organization, name, calendar_type, **fields):
    return baker.make(
        Calendar,
        organization=organization,
        name=name,
        external_id=f"rooms-{uuid.uuid4().hex[:8]}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=calendar_type,
        **fields,
    )


@pytest.fixture
def room(organization):
    return _calendar(organization, "Room 1", CalendarType.RESOURCE)


@pytest.fixture
def other_room(organization):
    return _calendar(organization, "Room 2", CalendarType.RESOURCE)


def _book(post, organization, appointment_type_with_slot, room_ids: list[int]) -> dict:
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
                "resourceCalendarIds": room_ids,
            }
        },
    )["data"]["createAppointmentTypeEvent"]


def _block(room, start_hour_minute: tuple[int, int], end_hour_minute: tuple[int, int]) -> None:
    """Block ``room`` on 2030-06-01, the day ``_book`` books 10:00-10:30 on."""
    baker.make(
        BlockedTime,
        organization=room.organization,
        calendar_fk=room,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, *start_hour_minute),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, *end_hour_minute),
        timezone="UTC",
    )


def _room_ids(organization, event_id: int) -> set[int]:
    return set(
        ResourceAllocation.objects.filter_by_organization(organization.id)
        .filter(event_fk_id=event_id)
        .values_list("calendar_fk_id", flat=True)
    )


def _update(post, organization, event_id: int, **fields) -> dict:
    return post(
        UPDATE_CALENDAR_EVENT_MUTATION,
        {"input": {"organizationId": organization.id, "eventId": event_id, **fields}},
    )


def test_booking_with_a_room_allocates_it(
    org_wide_post, organization, appointment_type_with_slot, room
):
    result = _book(org_wide_post, organization, appointment_type_with_slot, [room.id, room.id])

    assert result["errorMessage"] is None
    assert _room_ids(organization, int(result["event"]["id"])) == {room.id}


@pytest.mark.parametrize("bad_room", ["foreign", "personal", "disabled"])
def test_booking_with_a_calendar_that_is_not_a_bookable_room_is_refused(
    org_wide_post, organization, appointment_type_with_slot, bad_room
):
    calendars = {
        "foreign": lambda: _calendar(
            baker.make(Organization, name="Other Org"), "Foreign Room", CalendarType.RESOURCE
        ),
        "personal": lambda: _calendar(organization, "Someone", CalendarType.PERSONAL),
        "disabled": lambda: _calendar(
            organization,
            "Closed Room",
            CalendarType.RESOURCE,
            visibility=CalendarVisibility.INACTIVE,
        ),
    }
    calendar = calendars[bad_room]()

    result = _book(org_wide_post, organization, appointment_type_with_slot, [calendar.id])

    assert result == {"success": False, "errorMessage": "Room not found.", "event": None}
    assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


@pytest.mark.parametrize("change", ["omitted", "empty-list", "new-list"])
def test_update_sets_the_rooms(
    org_wide_post, organization, appointment_type_with_slot, room, other_room, change
):
    """Omitting the field keeps the rooms, an empty list clears them, a new list replaces them."""
    fields, expected = {
        "omitted": ({"title": "Renamed"}, {room.id}),
        "empty-list": ({"resourceCalendarIds": []}, set()),
        "new-list": ({"resourceCalendarIds": [other_room.id]}, {other_room.id}),
    }[change]
    booked = _book(org_wide_post, organization, appointment_type_with_slot, [room.id])
    event_id = int(booked["event"]["id"])

    updated = _update(org_wide_post, organization, event_id, **fields)

    assert updated.get("errors") is None
    assert _room_ids(organization, event_id) == expected


def test_update_with_another_organizations_room_is_refused(
    org_wide_post, organization, appointment_type_with_slot, room
):
    booked = _book(org_wide_post, organization, appointment_type_with_slot, [room.id])
    event_id = int(booked["event"]["id"])
    foreign_room = _calendar(
        baker.make(Organization, name="Other Org"), "Foreign Room", CalendarType.RESOURCE
    )

    updated = _update(org_wide_post, organization, event_id, resourceCalendarIds=[foreign_room.id])

    assert [e["message"] for e in updated["errors"]] == ["Room not found."]
    assert _room_ids(organization, event_id) == {room.id}


def test_booking_with_a_room_taken_at_that_time_is_refused(
    org_wide_post, organization, appointment_type_with_slot, room
):
    _block(room, (10, 15), (10, 45))

    result = _book(org_wide_post, organization, appointment_type_with_slot, [room.id])

    assert result == {
        "success": False,
        "errorMessage": "Room is not available at the requested time.",
        "event": None,
    }
    assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


def test_update_adding_a_room_taken_at_the_event_time_is_refused(
    org_wide_post, organization, appointment_type_with_slot, room, other_room
):
    booked = _book(org_wide_post, organization, appointment_type_with_slot, [room.id])
    event_id = int(booked["event"]["id"])
    _block(other_room, (10, 0), (10, 30))

    updated = _update(
        org_wide_post, organization, event_id, resourceCalendarIds=[room.id, other_room.id]
    )

    assert [e["message"] for e in updated["errors"]] == [
        "Room is not available at the requested time."
    ]
    assert _room_ids(organization, event_id) == {room.id}


def test_update_keeps_a_room_disabled_after_it_was_booked(
    org_wide_post, organization, appointment_type_with_slot, room
):
    """Rooms the event already holds are not checked again, so disabling a room does
    not block later changes to the events that booked it."""
    booked = _book(org_wide_post, organization, appointment_type_with_slot, [room.id])
    event_id = int(booked["event"]["id"])
    Calendar.objects.filter_by_organization(organization.id).filter(id=room.id).update(
        visibility=CalendarVisibility.INACTIVE
    )

    updated = _update(org_wide_post, organization, event_id, title="Renamed")

    assert updated.get("errors") is None
    assert _room_ids(organization, event_id) == {room.id}
