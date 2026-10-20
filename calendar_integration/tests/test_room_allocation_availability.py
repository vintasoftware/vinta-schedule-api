"""A room held by an event on another calendar is taken for every availability check.

An event booked with rooms keeps them as resource allocations and writes nothing on
the rooms' own calendars. Both shared availability paths must still see the room as
taken: the calendar queryset filter (``only_calendars_available_in_ranges`` and its
bulk-modification twin), which appointment bookings check selected calendars with,
and ``slot_engine.fetch_blocking_spans``, which slot discovery walks.
"""

import datetime
import uuid

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import AvailableTime, Calendar, CalendarEvent, ResourceAllocation
from calendar_integration.services import slot_engine
from organizations.models import Organization


pytestmark = pytest.mark.django_db

START = datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC)
END = datetime.datetime(2030, 6, 1, 10, 30, tzinfo=datetime.UTC)


@pytest.fixture
def organization():
    return Organization.objects.create(name="Rooms Org", should_sync_rooms=False)


def _calendar(organization, name, calendar_type, *, managed=False) -> Calendar:
    calendar = baker.make(
        Calendar,
        organization=organization,
        name=name,
        external_id=f"rooms-{uuid.uuid4().hex[:8]}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=calendar_type,
        manage_available_windows=managed,
    )
    if managed:
        AvailableTime.objects.create(
            organization=organization,
            calendar=calendar,
            start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
            end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
            timezone="UTC",
        )
    return calendar


def _hold(room: Calendar, host_calendar: Calendar) -> CalendarEvent:
    """An event on ``host_calendar`` from START to END that holds ``room``."""
    event = CalendarEvent.objects.create(
        organization=host_calendar.organization,
        calendar_fk=host_calendar,
        title="Meeting",
        start_time_tz_unaware=START.replace(tzinfo=None),
        end_time_tz_unaware=END.replace(tzinfo=None),
        timezone="UTC",
    )
    ResourceAllocation.objects.create(organization=room.organization, event=event, calendar_fk=room)
    return event


@pytest.mark.parametrize("managed", [False, True], ids=["unmanaged-room", "managed-room"])
@pytest.mark.parametrize(
    "filter_name",
    [
        "only_calendars_available_in_ranges",
        "only_calendars_available_in_ranges_with_bulk_modifications",
    ],
)
def test_a_room_held_by_another_calendars_event_is_not_available(
    organization, managed, filter_name
):
    host_calendar = _calendar(organization, "Host", CalendarType.PERSONAL)
    held_room = _calendar(organization, "Room 1", CalendarType.RESOURCE, managed=managed)
    free_room = _calendar(organization, "Room 2", CalendarType.RESOURCE, managed=managed)
    _hold(held_room, host_calendar)

    rooms = Calendar.objects.filter_by_organization(organization.id).filter(
        id__in=[held_room.id, free_room.id]
    )
    available = getattr(rooms, filter_name)([(START, END)])

    assert set(available.values_list("id", flat=True)) == {free_room.id}


@pytest.mark.parametrize("with_bulk_modifications", [False, True])
def test_blocking_spans_include_the_events_that_hold_a_room(organization, with_bulk_modifications):
    host_calendar = _calendar(organization, "Host", CalendarType.PERSONAL)
    room = _calendar(organization, "Room 1", CalendarType.RESOURCE)
    _hold(room, host_calendar)

    spans = slot_engine.fetch_blocking_spans(
        organization.id,
        {host_calendar.id, room.id},
        START - datetime.timedelta(hours=1),
        END + datetime.timedelta(hours=1),
        with_bulk_modifications=with_bulk_modifications,
    )

    # Compared as sets: an event's occurrence annotation also lists the event itself,
    # so each span appears twice, which no overlap check minds.
    assert {calendar_id: set(found) for calendar_id, found in spans.items()} == {
        host_calendar.id: {(START, END)},
        room.id: {(START, END)},
    }
