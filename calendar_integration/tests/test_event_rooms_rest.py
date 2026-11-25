"""Rooms on the internal REST event API (``CalendarEventViewSet``).

A room an event is given must be free for every occurrence the event will have, the
same check the public API and appointment-type booking use:

- creating an event, single or recurring, with a room taken at one of its
  occurrences is refused with a 400 and writes nothing;
- an update that adds a room is checked at the event's new time and rule;
- a room the event already holds is not checked again on an update that does not
  move it, so an existing double booking does not block a rename.

Rooms that are not active rooms of the organization never get this far: the
serializer's ``calendar`` field only accepts those.
"""

import datetime
import uuid

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarManagementToken,
    CalendarManagementTokenPermission,
    CalendarOwnership,
    ResourceAllocation,
)
from calendar_integration.services.calendar_permission_service import (
    DEFAULT_CALENDAR_OWNER_PERMISSIONS,
)
from organizations.models import Organization, OrganizationMembership
from users.models import Profile, User


pytestmark = pytest.mark.django_db

ROOM_TAKEN = {"non_field_errors": ["Room is not available at the requested time."]}


def _at(day: int, hour: int, minute: int = 0) -> datetime.datetime:
    """A time in June 2030; the 3rd is a Monday."""
    return datetime.datetime(2030, 6, day, hour, minute, tzinfo=datetime.UTC)


def _calendar(organization, name, calendar_type) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        name=name,
        external_id=f"rest-rooms-{uuid.uuid4().hex[:8]}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=calendar_type,
    )


@pytest.fixture
def organization():
    return Organization.objects.create(name="REST Rooms Org", should_sync_rooms=False)


@pytest.fixture
def room(organization):
    return _calendar(organization, "Room 1", CalendarType.RESOURCE)


@pytest.fixture
def host_calendar(organization):
    return _calendar(organization, "Host", CalendarType.PERSONAL)


@pytest.fixture
def member(organization, host_calendar) -> User:
    """A member who owns ``host_calendar``."""
    user = User.objects.create_user(email=f"rest-rooms-{uuid.uuid4().hex[:8]}@example.com")
    Profile.objects.create(user=user)
    OrganizationMembership.objects.create(user=user, organization=organization, is_active=True)
    CalendarOwnership.objects.create(
        calendar=host_calendar, membership_user_id=user.id, organization=organization
    )
    token = CalendarManagementToken.objects.create(
        calendar_fk=host_calendar, membership_user_id=user.id, organization=organization
    )
    for permission in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        CalendarManagementTokenPermission.objects.create(
            token_fk=token, permission=permission, organization=organization
        )
    return user


@pytest.fixture
def client(member):
    api_client = APIClient()
    api_client.force_authenticate(user=member)
    return api_client


def _grant_event_permissions(member, event) -> None:
    """``update_event`` resolves a user's permissions from an event-level token, which
    the API's create does not mint; ``test_event_identifier_rest`` does the same."""
    token = CalendarManagementToken.objects.create(
        event_fk=event, membership_user_id=member.id, organization=event.organization
    )
    for permission in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        CalendarManagementTokenPermission.objects.create(
            token_fk=token, permission=permission, organization=event.organization
        )


def _hold(room, calendar, start, end, title="Other meeting") -> CalendarEvent:
    """An event on ``calendar`` that holds ``room``."""
    event = CalendarEvent.objects.create(
        organization=calendar.organization,
        calendar_fk=calendar,
        title=title,
        start_time_tz_unaware=start.replace(tzinfo=None),
        end_time_tz_unaware=end.replace(tzinfo=None),
        timezone="UTC",
    )
    ResourceAllocation.objects.create(organization=room.organization, event=event, calendar_fk=room)
    return event


def _payload(calendar, start, end, rooms, **extra) -> dict:
    return {
        "organization": calendar.organization_id,
        "calendar": calendar.id,
        "title": "Planning",
        "description": "",
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "timezone": "UTC",
        "resource_allocations": [{"calendar": room.id} for room in rooms],
        "attendances": [],
        "external_attendances": [],
        **extra,
    }


def _create(client, calendar, start, end, rooms, **extra):
    return client.post(
        reverse("api:CalendarEvents-list"),
        _payload(calendar, start, end, rooms, **extra),
        format="json",
    )


def _patch(client, event, **fields):
    return client.patch(
        reverse("api:CalendarEvents-detail", kwargs={"pk": event.id}), fields, format="json"
    )


def _events_titled(organization, title) -> int:
    return CalendarEvent.objects.filter_by_organization(organization.id).filter(title=title).count()


def test_creating_an_event_with_a_free_room_books_it(client, organization, room, host_calendar):
    response = _create(client, host_calendar, _at(3, 10), _at(3, 11), [room])

    assert response.status_code == status.HTTP_201_CREATED, response.content
    assert (
        ResourceAllocation.objects.filter_by_organization(organization.id)
        .filter(event_fk_id=response.json()["id"], calendar_fk_id=room.id)
        .exists()
    )


def test_creating_an_event_with_a_taken_room_is_refused(client, organization, room, host_calendar):
    _hold(room, host_calendar, _at(3, 10, 30), _at(3, 11, 30))

    response = _create(client, host_calendar, _at(3, 10), _at(3, 11), [room])

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == ROOM_TAKEN
    assert _events_titled(organization, "Planning") == 0


def test_creating_a_series_whose_later_occurrence_takes_a_taken_room_is_refused(
    client, organization, room, host_calendar
):
    """Four Mondays from the 3rd; the room is taken on the third, the 17th."""
    _hold(room, host_calendar, _at(17, 10), _at(17, 11))

    response = _create(
        client,
        host_calendar,
        _at(3, 10),
        _at(3, 10, 30),
        [room],
        rrule_string="FREQ=WEEKLY;COUNT=4",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == ROOM_TAKEN
    assert _events_titled(organization, "Planning") == 0


def test_creating_a_series_that_ends_before_the_taken_room_books_it(client, room, host_calendar):
    _hold(room, host_calendar, _at(17, 10), _at(17, 11))

    response = _create(
        client,
        host_calendar,
        _at(3, 10),
        _at(3, 10, 30),
        [room],
        rrule_string="FREQ=WEEKLY;COUNT=2",
    )

    assert response.status_code == status.HTTP_201_CREATED, response.content


def _booked(client, member, calendar, start, end, rooms) -> CalendarEvent:
    """An event created through the API by ``member``, who may then update it."""
    response = _create(client, calendar, start, end, rooms)
    assert response.status_code == status.HTTP_201_CREATED, response.content
    event = CalendarEvent.objects.filter_by_organization(calendar.organization_id).get(
        id=response.json()["id"]
    )
    _grant_event_permissions(member, event)
    return event


def test_adding_a_room_taken_at_the_event_time_is_refused(
    client, member, organization, room, host_calendar
):
    event = _booked(client, member, host_calendar, _at(3, 10), _at(3, 11), [])
    _hold(room, host_calendar, _at(3, 10, 30), _at(3, 11, 30))

    response = _patch(client, event, resource_allocations=[{"calendar": room.id}])

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == ROOM_TAKEN
    assert (
        not ResourceAllocation.objects.filter_by_organization(organization.id)
        .filter(event_fk_id=event.id)
        .exists()
    )


def test_a_room_added_in_a_move_is_checked_at_the_new_time(client, member, room, host_calendar):
    """Free at the event's old time, 10:00, taken at its new time, 14:00."""
    event = _booked(client, member, host_calendar, _at(3, 10), _at(3, 11), [])
    _hold(room, host_calendar, _at(3, 14), _at(3, 15))

    response = _patch(
        client,
        event,
        start_time=_at(3, 14).isoformat(),
        end_time=_at(3, 15).isoformat(),
        resource_allocations=[{"calendar": room.id}],
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == ROOM_TAKEN


def test_renaming_an_event_does_not_recheck_a_room_it_holds(client, member, room, host_calendar):
    """The room gets double-booked at the event's own time after it was booked."""
    event = _booked(client, member, host_calendar, _at(3, 10), _at(3, 11), [room])
    _hold(room, host_calendar, _at(3, 10), _at(3, 11), title="Double booking")

    response = _patch(client, event, title="Renamed")

    assert response.status_code == status.HTTP_200_OK, response.content
