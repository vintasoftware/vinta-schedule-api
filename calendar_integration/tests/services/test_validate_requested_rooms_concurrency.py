"""Two bookings of the same room at the same time, racing on a real database.

Mirrors ``test_availability_limit_concurrency.py``: ``transaction=True`` and two OS
threads holding two separate connections, so the race can only fail when two
transactions are open at once.

Each thread opens a transaction, validates the room, sleeps, then writes an event that
holds the room. The sleep stands in for the work a real booking does between the check
and the write, and makes the race deterministic. ``validate_requested_rooms`` locks the
room, so the second thread waits for the first to commit, then sees its allocation and
is refused.
"""

import datetime
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.db import connection, transaction

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.exceptions import RoomUnavailableError
from calendar_integration.models import Calendar, CalendarEvent, ResourceAllocation
from calendar_integration.querysets import CalendarQuerySet
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization


START = datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC)
END = datetime.datetime(2030, 6, 1, 10, 30, tzinfo=datetime.UTC)

BARRIER_TIMEOUT_SECONDS = 10
THREAD_JOIN_TIMEOUT_SECONDS = 30
# Long enough that two unlocked bookings reliably both pass the check before either
# writes; short enough not to slow the suite noticeably.
RACE_WINDOW_SECONDS = 0.5


def _calendar(organization: Organization, name: str, calendar_type: str) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        name=name,
        external_id=f"rooms-{uuid.uuid4().hex[:8]}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=calendar_type,
    )


def _book_the_room_twice_at_once() -> tuple[list[str], Organization, Calendar]:
    """Two threads book the same room for ``[START, END)``. Returns each thread's
    outcome (``"ok"`` or ``"unavailable"``), the organization, and the room."""
    organization = Organization.objects.create(name="Rooms Org", should_sync_rooms=False)
    room = _calendar(organization, "Room 1", CalendarType.RESOURCE)
    host_calendar = _calendar(organization, "Host", CalendarType.PERSONAL)
    start_barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT_SECONDS)

    def book() -> str:
        try:
            start_barrier.wait()
            service = CalendarService()
            service.initialize_without_provider(organization=organization)
            with transaction.atomic():
                try:
                    service.validate_requested_rooms([room.id], START, END)
                except RoomUnavailableError:
                    return "unavailable"
                time.sleep(RACE_WINDOW_SECONDS)
                event = CalendarEvent.objects.create(
                    organization=organization,
                    calendar_fk=host_calendar,
                    title="Meeting",
                    start_time_tz_unaware=START.replace(tzinfo=None),
                    end_time_tz_unaware=END.replace(tzinfo=None),
                    timezone="UTC",
                )
                ResourceAllocation.objects.create(
                    organization=organization, event=event, calendar_fk=room
                )
            return "ok"
        finally:
            # Each thread owns its own connection; leaking it holds the room lock past
            # the test and wedges the next one.
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(book) for _ in range(2)]
        # `result(timeout=...)` re-raises whatever the thread raised, on this thread,
        # and turns a deadlock into a failure instead of a hung suite.
        outcomes = [future.result(timeout=THREAD_JOIN_TIMEOUT_SECONDS) for future in futures]
    return outcomes, organization, room


def _allocation_count(organization: Organization, room: Calendar) -> int:
    return (
        ResourceAllocation.objects.filter_by_organization(organization.id)
        .filter(calendar_fk_id=room.id)
        .count()
    )


@pytest.mark.django_db(transaction=True)
def test_two_bookings_of_the_same_room_at_once_let_only_one_through():
    outcomes, organization, room = _book_the_room_twice_at_once()

    assert sorted(outcomes) == ["ok", "unavailable"]
    assert _allocation_count(organization, room) == 1


@pytest.mark.django_db(transaction=True)
def test_without_the_lock_both_bookings_take_the_room():
    """Proof that the test above races for real.

    With ``select_for_update`` turned into a no-op, both threads read the room as free
    and both write an allocation. If this test starts failing, the harness has stopped
    racing and the test above proves nothing.
    """
    with patch.object(CalendarQuerySet, "select_for_update", lambda self, **kwargs: self):
        outcomes, organization, room = _book_the_room_twice_at_once()

    assert outcomes == ["ok", "ok"]
    assert _allocation_count(organization, room) == 2
