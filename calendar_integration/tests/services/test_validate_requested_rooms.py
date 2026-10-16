"""``CalendarService.validate_requested_rooms``: the check a caller's requested rooms go
through before they are booked, and ``validate_room_changes``, which sends it only the
rooms an event does not hold yet.

A requested id must be an active room (resource calendar) of the organization, and the
room must be free for the whole requested time: no event of its own calendar, no blocked
time on it, and no other event holding it, a recurring one included. Touching spans do
not overlap.
"""

import datetime
import uuid

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType, CalendarVisibility
from calendar_integration.exceptions import RoomNotFoundError, RoomUnavailableError
from calendar_integration.factories import CalendarEventFactory
from calendar_integration.models import BlockedTime, Calendar, CalendarEvent, ResourceAllocation
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization


pytestmark = pytest.mark.django_db

START = datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC)
END = datetime.datetime(2030, 6, 1, 10, 30, tzinfo=datetime.UTC)


def _naive(value: datetime.datetime) -> datetime.datetime:
    return value.replace(tzinfo=None)


def _calendar(organization, name, calendar_type, **fields) -> Calendar:
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
def organization():
    return Organization.objects.create(name="Rooms Org", should_sync_rooms=False)


@pytest.fixture
def room(organization):
    return _calendar(organization, "Room 1", CalendarType.RESOURCE)


@pytest.fixture
def host_calendar(organization):
    return _calendar(organization, "Host", CalendarType.PERSONAL)


@pytest.fixture
def service(organization):
    calendar_service = CalendarService()
    calendar_service.initialize_without_provider(organization=organization)
    return calendar_service


def _event_holding(room, host_calendar, start, end) -> CalendarEvent:
    event = CalendarEvent.objects.create(
        organization=host_calendar.organization,
        calendar_fk=host_calendar,
        title="Other meeting",
        start_time_tz_unaware=_naive(start),
        end_time_tz_unaware=_naive(end),
        timezone="UTC",
    )
    ResourceAllocation.objects.create(organization=room.organization, event=event, calendar_fk=room)
    return event


@pytest.mark.parametrize("bad_room", ["foreign", "personal", "disabled", "missing"])
def test_an_id_that_is_not_an_active_room_of_the_organization_is_not_found(
    service, organization, bad_room
):
    calendar_id = {
        "foreign": lambda: (
            _calendar(
                baker.make(Organization, name="Other Org"), "Foreign Room", CalendarType.RESOURCE
            ).id
        ),
        "personal": lambda: _calendar(organization, "Someone", CalendarType.PERSONAL).id,
        "disabled": lambda: (
            _calendar(
                organization,
                "Closed Room",
                CalendarType.RESOURCE,
                visibility=CalendarVisibility.INACTIVE,
            ).id
        ),
        "missing": lambda: 999_999,
    }[bad_room]()

    with pytest.raises(RoomNotFoundError):
        service.validate_requested_rooms([calendar_id], START, END)


@pytest.mark.parametrize("taken_by", ["other-event", "own-blocked-time", "own-event", "series"])
def test_a_room_taken_during_the_requested_time_is_unavailable(
    service, organization, room, host_calendar, taken_by
):
    overlapping_start = START + datetime.timedelta(minutes=15)
    overlapping_end = END + datetime.timedelta(minutes=15)
    if taken_by == "other-event":
        _event_holding(room, host_calendar, overlapping_start, overlapping_end)
    elif taken_by == "own-blocked-time":
        baker.make(
            BlockedTime,
            organization=organization,
            calendar_fk=room,
            start_time_tz_unaware=_naive(overlapping_start),
            end_time_tz_unaware=_naive(overlapping_end),
            timezone="UTC",
        )
    elif taken_by == "own-event":
        CalendarEvent.objects.create(
            organization=organization,
            calendar_fk=room,
            title="Booked at the provider",
            start_time_tz_unaware=_naive(overlapping_start),
            end_time_tz_unaware=_naive(overlapping_end),
            timezone="UTC",
        )
    else:
        # A daily series that started two days earlier; its third occurrence overlaps.
        series = CalendarEventFactory.create_recurring_event(
            calendar=host_calendar,
            title="Daily stand-up",
            description="",
            start_time=_naive(overlapping_start - datetime.timedelta(days=2)),
            end_time=_naive(overlapping_end - datetime.timedelta(days=2)),
            frequency="DAILY",
            count=5,
        )
        ResourceAllocation.objects.create(organization=organization, event=series, calendar_fk=room)

    with pytest.raises(RoomUnavailableError):
        service.validate_requested_rooms([room.id], START, END)


@pytest.mark.parametrize("side", ["before", "after"])
def test_a_booking_that_only_touches_the_requested_time_leaves_the_room_free(
    service, room, host_calendar, side
):
    if side == "before":
        _event_holding(room, host_calendar, START - datetime.timedelta(minutes=30), START)
    else:
        _event_holding(room, host_calendar, END, END + datetime.timedelta(minutes=30))

    assert service.validate_requested_rooms([room.id], START, END) is None


def test_no_requested_rooms_is_valid(service):
    assert service.validate_requested_rooms([], START, END) is None


def test_room_changes_skip_rooms_the_event_already_holds(
    service, organization, room, host_calendar
):
    """A held room is kept even when it is disabled and taken by another event since."""
    event = _event_holding(room, host_calendar, START, END)
    _event_holding(room, host_calendar, START, END)
    Calendar.objects.filter_by_organization(organization.id).filter(id=room.id).update(
        visibility=CalendarVisibility.INACTIVE
    )

    assert service.validate_room_changes(event, [room.id], START, END) is None


def test_room_changes_check_the_rooms_the_event_does_not_hold_yet(
    service, organization, room, host_calendar
):
    event = _event_holding(room, host_calendar, START, END)
    other_room = _calendar(organization, "Room 2", CalendarType.RESOURCE)
    _event_holding(other_room, host_calendar, START, END)

    with pytest.raises(RoomUnavailableError):
        service.validate_room_changes(event, [room.id, other_room.id], START, END)
