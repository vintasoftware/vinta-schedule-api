"""Moving an event checks that the rooms it keeps are free at the new time.

``update_event`` and ``reschedule_event_occurrence`` are the paths every reschedule
goes through (the public API, booking codes, the internal REST API, appointment types).
The rooms an event already holds are checked at its new time, ignoring the event
itself, so a move that overlaps its own old slot is fine. They are not checked as
rooms again: a room disabled after it was booked stays held. An update that does not
move the event does not check them at all.
"""

import datetime
import uuid

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType, CalendarVisibility
from calendar_integration.exceptions import RoomUnavailableError
from calendar_integration.factories import CalendarEventFactory
from calendar_integration.models import Calendar, CalendarEvent, ResourceAllocation
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    CalendarEventInputData,
    ResourceAllocationInputData,
)
from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


pytestmark = pytest.mark.django_db

TEN = datetime.datetime(2030, 6, 3, 10, 0, tzinfo=datetime.UTC)


def _at(hour: int, minute: int = 0, day: int = 3) -> datetime.datetime:
    return datetime.datetime(2030, 6, day, hour, minute, tzinfo=datetime.UTC)


def _naive(value: datetime.datetime) -> datetime.datetime:
    return value.replace(tzinfo=None)


def _calendar(organization, name, calendar_type, **fields) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        name=name,
        external_id=f"move-rooms-{uuid.uuid4().hex[:8]}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=calendar_type,
        **fields,
    )


@pytest.fixture
def organization():
    return Organization.objects.create(name="Move Rooms Org", should_sync_rooms=False)


@pytest.fixture
def room(organization):
    return _calendar(organization, "Room 1", CalendarType.RESOURCE)


@pytest.fixture
def host_calendar(organization):
    return _calendar(organization, "Host", CalendarType.PERSONAL)


@pytest.fixture
def service(organization):
    """A calendar service acting as an org-wide public API token."""
    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="move-rooms", organization=organization
    )
    ResourceAccess.objects.create(
        system_user=system_user, resource_name=PublicAPIResources.CALENDAR_EVENT
    )
    calendar_service = CalendarService()
    calendar_service.initialize_without_provider(
        user_or_token=system_user, organization=organization
    )
    return calendar_service


def _event_holding(room, calendar, start, end, title="Meeting") -> CalendarEvent:
    event = CalendarEvent.objects.create(
        organization=calendar.organization,
        calendar_fk=calendar,
        title=title,
        start_time_tz_unaware=_naive(start),
        end_time_tz_unaware=_naive(end),
        timezone="UTC",
    )
    ResourceAllocation.objects.create(organization=room.organization, event=event, calendar_fk=room)
    return event


def _move(service, event, room, start, end, title=None) -> CalendarEvent:
    return service.update_event(
        event.calendar_fk_id,
        event.id,
        CalendarEventInputData(
            title=title,
            description=None,
            start_time=start,
            end_time=end,
            timezone="UTC",
            resource_allocations=[ResourceAllocationInputData(resource_id=room.id)],
        ),
    )


def _start_of(event) -> datetime.datetime:
    stored = CalendarEvent.objects.filter_by_organization(event.organization_id).get(id=event.id)
    return _naive(stored.start_time_tz_unaware)


def test_moving_an_event_onto_a_time_its_room_is_taken_is_refused(service, room, host_calendar):
    event = _event_holding(room, host_calendar, _at(10), _at(10, 30))
    _event_holding(room, host_calendar, _at(14), _at(15), title="Other meeting")

    with pytest.raises(RoomUnavailableError):
        _move(service, event, room, _at(14), _at(14, 30))

    assert _start_of(event) == _naive(_at(10))


def test_moving_an_event_into_its_own_old_slot_is_not_a_conflict(service, room, host_calendar):
    event = _event_holding(room, host_calendar, _at(10), _at(10, 30))

    _move(service, event, room, _at(10, 15), _at(10, 45))

    assert _start_of(event) == _naive(_at(10, 15))


def test_an_update_that_does_not_move_the_event_does_not_check_its_rooms(
    service, room, host_calendar
):
    """The room is already double-booked at the event's own time; renaming the event
    must still work."""
    event = _event_holding(room, host_calendar, _at(10), _at(10, 30))
    _event_holding(room, host_calendar, _at(10), _at(10, 30), title="Double booking")

    _move(service, event, room, _at(10), _at(10, 30), title="Renamed")

    assert CalendarEvent.objects.filter_by_organization(event.organization_id).get(
        id=event.id
    ).title == ("Renamed")


def test_a_room_disabled_after_booking_does_not_stop_the_move(service, organization, host_calendar):
    room = _calendar(organization, "Closing Room", CalendarType.RESOURCE)
    event = _event_holding(room, host_calendar, _at(10), _at(10, 30))
    room.visibility = CalendarVisibility.INACTIVE
    room.save(update_fields=["visibility"])

    _move(service, event, room, _at(14), _at(14, 30))

    assert _start_of(event) == _naive(_at(14))


def test_moving_one_occurrence_onto_a_time_the_series_room_is_taken_is_refused(
    service, room, host_calendar
):
    series = CalendarEventFactory.create_recurring_event(
        calendar=host_calendar,
        title="Daily stand-up",
        description="",
        start_time=_naive(_at(10, day=1)),
        end_time=_naive(_at(10, 30, day=1)),
        frequency="DAILY",
        count=5,
    )
    ResourceAllocation.objects.create(
        organization=host_calendar.organization, event=series, calendar_fk=room
    )
    _event_holding(room, host_calendar, _at(14), _at(15), title="Other meeting")

    with pytest.raises(RoomUnavailableError):
        service.reschedule_event_occurrence(
            calendar_id=host_calendar.id,
            master_event_id=series.id,
            recurrence_id=_at(10, day=3),
            start_time=_at(14),
            end_time=_at(14, 30),
            timezone="UTC",
        )

    # Moving it within its own slot is fine.
    occurrence = service.reschedule_event_occurrence(
        calendar_id=host_calendar.id,
        master_event_id=series.id,
        recurrence_id=_at(10, day=3),
        start_time=_at(10, 15),
        end_time=_at(10, 45),
        timezone="UTC",
    )
    assert _naive(occurrence.start_time_tz_unaware) == _naive(_at(10, 15))


def test_moving_an_already_moved_occurrence_by_its_own_id_checks_the_series_room(
    service, room, host_calendar
):
    """A moved occurrence's own row holds no rooms; the series holds them. Moving that
    row again through ``update_event`` must still check the series' room."""
    series = CalendarEventFactory.create_recurring_event(
        calendar=host_calendar,
        title="Daily stand-up",
        description="",
        start_time=_naive(_at(10, day=1)),
        end_time=_naive(_at(10, 30, day=1)),
        frequency="DAILY",
        count=5,
    )
    ResourceAllocation.objects.create(
        organization=host_calendar.organization, event=series, calendar_fk=room
    )
    _event_holding(room, host_calendar, _at(14), _at(15), title="Other meeting")
    occurrence = service.reschedule_event_occurrence(
        calendar_id=host_calendar.id,
        master_event_id=series.id,
        recurrence_id=_at(10, day=3),
        start_time=_at(10, 15),
        end_time=_at(10, 45),
        timezone="UTC",
    )

    with pytest.raises(RoomUnavailableError):
        service.update_event(
            host_calendar.id,
            occurrence.id,
            CalendarEventInputData(
                title=None,
                description=None,
                start_time=_at(14),
                end_time=_at(14, 30),
                timezone="UTC",
                resource_allocations=[],
            ),
        )

    assert _start_of(occurrence) == _naive(_at(10, 15))
