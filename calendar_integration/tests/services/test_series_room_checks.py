"""Room checks cover every occurrence of a recurring series.

A series holds its rooms for each of its occurrences, so a series is checked over
all of them, not only the first:

- giving a series a room (``validate_room_changes``) refuses a room taken on any
  later occurrence;
- moving a series (``update_event``) refuses a new time at which a kept room is taken
  on any later occurrence, including occurrences a rule changed in the same update
  adds. Its own old occurrences do not count against it;
- changing only the rule checks the occurrences it adds. A rule that only drops
  occurrences, or the same rule spelled another way, checks nothing, so an existing
  double booking does not block it.

The occurrences come from the Postgres expansion, so the rule's COUNT ends the check,
a cancelled occurrence does not need the room, and occurrences already over are not
checked. ``ROOM_CHECK_LOOKAHEAD`` is the furthest a series is checked ahead.

A room is free by the same check as every other calendar
(``only_calendars_available_in_ranges``): it is taken by its own events and blocked
times, recurring ones included, and by events elsewhere that hold it; a room that
manages its availability windows is free only inside them.
"""

import datetime
import uuid

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.exceptions import RoomUnavailableError
from calendar_integration.factories import CalendarEventFactory
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    EventRecurrenceException,
    RecurrenceRule,
    ResourceAllocation,
)
from calendar_integration.services.calendar_event_service import EventSchedule
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


def _at(day: int, hour: int, minute: int = 0, month: int = 6, year: int = 2030):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC)


def _naive(value: datetime.datetime) -> datetime.datetime:
    return value.replace(tzinfo=None)


def _calendar(organization, name, calendar_type) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        name=name,
        external_id=f"series-rooms-{uuid.uuid4().hex[:8]}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=calendar_type,
    )


@pytest.fixture
def organization():
    return Organization.objects.create(name="Series Rooms Org", should_sync_rooms=False)


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
        integration_name="series-rooms", organization=organization
    )
    ResourceAccess.objects.create(
        system_user=system_user, resource_name=PublicAPIResources.CALENDAR_EVENT
    )
    calendar_service = CalendarService()
    calendar_service.initialize_without_provider(
        user_or_token=system_user, organization=organization
    )
    return calendar_service


def _hold(room, event) -> None:
    ResourceAllocation.objects.create(organization=room.organization, event=event, calendar_fk=room)


def _series(calendar, start, end, *, frequency="WEEKLY", count=4, room=None, title="Weekly"):
    """A series on ``calendar``; 2030-06-03 is a Monday, so the default runs on the
    Mondays 3, 10, 17 and 24 of June 2030."""
    series = CalendarEventFactory.create_recurring_event(
        calendar=calendar,
        title=title,
        description="",
        start_time=_naive(start),
        end_time=_naive(end),
        frequency=frequency,
        count=count,
    )
    if room is not None:
        _hold(room, series)
    return series


def _event_holding(room, calendar, start, end) -> CalendarEvent:
    event = CalendarEvent.objects.create(
        organization=calendar.organization,
        calendar_fk=calendar,
        title="Other meeting",
        start_time_tz_unaware=_naive(start),
        end_time_tz_unaware=_naive(end),
        timezone="UTC",
    )
    _hold(room, event)
    return event


def _move(service, series, room, start, end, rrule="FREQ=WEEKLY;COUNT=4") -> CalendarEvent:
    return service.update_event(
        series.calendar_fk_id,
        series.id,
        CalendarEventInputData(
            title=None,
            description=None,
            start_time=start,
            end_time=end,
            timezone="UTC",
            resource_allocations=[ResourceAllocationInputData(resource_id=room.id)],
            recurrence_rule=rrule,
        ),
    )


def _stored(series) -> CalendarEvent:
    return (
        CalendarEvent.objects.filter_by_organization(series.organization_id)
        .select_related("recurrence_rule")
        .get(id=series.id)
    )


# --- taking the room on the third occurrence, 2030-06-17 11:00-12:00 ----------------


def _taken_by_an_event(room, calendar):
    _event_holding(room, calendar, _at(17, 11), _at(17, 12))


def _taken_by_a_blocked_time(room, calendar):
    baker.make(
        BlockedTime,
        organization=room.organization,
        calendar_fk=room,
        start_time_tz_unaware=_naive(_at(17, 11)),
        end_time_tz_unaware=_naive(_at(17, 12)),
        timezone="UTC",
    )


def _taken_by_another_series(room, calendar):
    """A daily series from Saturday 15th: only its third occurrence lands on Monday."""
    _series(
        calendar, _at(15, 11), _at(15, 12), frequency="DAILY", count=3, room=room, title="Daily"
    )


def _taken_by_a_recurring_blocked_time(room, calendar):
    """A daily block from Saturday 15th: only its third occurrence lands on Monday."""
    rule = RecurrenceRule.objects.create(
        organization=room.organization, frequency="DAILY", interval=1, count=3
    )
    baker.make(
        BlockedTime,
        organization=room.organization,
        calendar_fk=room,
        start_time_tz_unaware=_naive(_at(15, 11)),
        end_time_tz_unaware=_naive(_at(15, 12)),
        timezone="UTC",
        recurrence_rule_fk=rule,
    )


@pytest.mark.parametrize(
    "take_the_room",
    [
        _taken_by_an_event,
        _taken_by_a_blocked_time,
        _taken_by_another_series,
        _taken_by_a_recurring_blocked_time,
    ],
    ids=["event", "blocked-time", "another-series", "recurring-blocked-time"],
)
def test_moving_a_series_onto_a_time_its_room_is_taken_on_a_later_occurrence_is_refused(
    service, room, host_calendar, take_the_room
):
    series = _series(host_calendar, _at(3, 10), _at(3, 10, 30), room=room)
    take_the_room(room, host_calendar)

    with pytest.raises(RoomUnavailableError):
        _move(service, series, room, _at(3, 11), _at(3, 11, 30))

    assert _naive(_stored(series).start_time_tz_unaware) == _naive(_at(3, 10))


def test_moving_a_series_with_no_collision_moves_it(service, room, host_calendar):
    series = _series(host_calendar, _at(3, 10), _at(3, 10, 30), room=room)
    _taken_by_an_event(room, host_calendar)

    # Back to back with the other meeting on the 17th: touching is not overlapping.
    _move(service, series, room, _at(3, 12), _at(3, 12, 30))

    assert _naive(_stored(series).start_time_tz_unaware) == _naive(_at(3, 12))


def test_moving_a_series_over_its_own_old_occurrences_is_not_a_conflict(
    service, room, host_calendar
):
    series = _series(host_calendar, _at(3, 10), _at(3, 10, 30), room=room)

    _move(service, series, room, _at(3, 10, 15), _at(3, 10, 45))

    assert _naive(_stored(series).start_time_tz_unaware) == _naive(_at(3, 10, 15))


def test_a_collision_after_the_series_ends_does_not_count(service, room, host_calendar):
    series = _series(host_calendar, _at(3, 10), _at(3, 10, 30), room=room)
    # The fifth Monday; the series stops after four.
    _event_holding(room, host_calendar, _at(1, 11, month=7), _at(1, 12, month=7))

    _move(service, series, room, _at(3, 11), _at(3, 11, 30))

    assert _naive(_stored(series).start_time_tz_unaware) == _naive(_at(3, 11))


def test_occurrences_a_new_rule_adds_in_the_same_move_are_checked(service, room, host_calendar):
    series = _series(host_calendar, _at(3, 10), _at(3, 10, 30), count=2, room=room)
    _taken_by_an_event(room, host_calendar)

    with pytest.raises(RoomUnavailableError):
        _move(service, series, room, _at(3, 11), _at(3, 11, 30), rrule="FREQ=WEEKLY;COUNT=4")

    # The check expanded the new rule without keeping it.
    stored = _stored(series)
    assert stored.recurrence_rule.count == 2
    assert _naive(stored.start_time_tz_unaware) == _naive(_at(3, 10))

    # Under its own rule the series ends before the 17th.
    _move(service, series, room, _at(3, 11), _at(3, 11, 30), rrule="FREQ=WEEKLY;COUNT=2")
    assert _naive(_stored(series).start_time_tz_unaware) == _naive(_at(3, 11))


def test_extending_the_rule_without_moving_checks_the_added_occurrences(
    service, room, host_calendar
):
    series = _series(host_calendar, _at(3, 11), _at(3, 11, 30), count=2, room=room)
    _taken_by_an_event(room, host_calendar)

    with pytest.raises(RoomUnavailableError):
        _move(service, series, room, _at(3, 11), _at(3, 11, 30), rrule="FREQ=WEEKLY;COUNT=4")

    assert _stored(series).recurrence_rule.count == 2


@pytest.mark.parametrize(
    "rrule",
    ["FREQ=WEEKLY;COUNT=2", "RRULE:FREQ=WEEKLY;COUNT=4"],
    ids=["shortened", "same-rule-spelled-differently"],
)
def test_a_rule_change_that_adds_no_occurrence_does_not_check_the_rooms(
    service, room, host_calendar, rrule
):
    """The room is already double-booked on the 10th, which the series keeps either
    way; the update must still go through."""
    series = _series(host_calendar, _at(3, 11), _at(3, 11, 30), room=room)
    _event_holding(room, host_calendar, _at(10, 11), _at(10, 12))

    _move(service, series, room, _at(3, 11), _at(3, 11, 30), rrule=rrule)

    assert _stored(series).recurrence_rule.to_rrule_string() == rrule.removeprefix("RRULE:")


@pytest.mark.parametrize(
    ("change", "differs"),
    [
        ({}, False),
        ({"recurrence_rule": "RRULE:FREQ=WEEKLY;COUNT=4"}, False),
        ({"recurrence_rule": "FREQ=WEEKLY;COUNT=5"}, True),
        ({"start_time": _at(3, 11, 5)}, True),
        ({"iana_tz": "America/Sao_Paulo"}, True),
    ],
    ids=["same", "same-rule-spelled-differently", "new-rule", "new-time", "new-timezone"],
)
def test_the_requested_schedule_equals_the_stored_one_only_when_nothing_moves(
    service, host_calendar, change, differs
):
    """``update_event`` checks rooms only when the two differ, so an update that keeps
    the event where it is must compare equal."""
    series = _series(host_calendar, _at(3, 11), _at(3, 11, 30))
    requested = {
        "start_time": _at(3, 11),
        "end_time": _at(3, 11, 30),
        "iana_tz": "UTC",
        "recurrence_rule": "FREQ=WEEKLY;COUNT=4",
        **change,
    }

    schedule = service._get_event_service()._requested_schedule(
        requested.pop("start_time"), requested.pop("end_time"), **requested
    )

    assert (schedule != EventSchedule.stored(_stored(series))) is differs


def test_adding_a_room_taken_on_a_later_occurrence_is_refused(service, room, host_calendar):
    series = _series(host_calendar, _at(3, 11), _at(3, 11, 30))
    _taken_by_an_event(room, host_calendar)

    with pytest.raises(RoomUnavailableError):
        service.validate_room_changes(series, [room.id], series.start_time, series.end_time)


def test_a_room_that_manages_its_windows_is_free_only_inside_them(
    service, organization, room, host_calendar
):
    """Open on Mondays 08:00-12:00 from the 3rd to the 10th only."""
    rule = RecurrenceRule.objects.create(
        organization=organization, frequency="WEEKLY", interval=1, count=2
    )
    AvailableTime.objects.create(
        organization=organization,
        calendar=room,
        start_time_tz_unaware=_naive(_at(3, 8)),
        end_time_tz_unaware=_naive(_at(3, 12)),
        timezone="UTC",
        recurrence_rule_fk=rule,
    )
    room.manage_available_windows = True
    room.save(update_fields=["manage_available_windows"])

    two_mondays = _series(host_calendar, _at(3, 11), _at(3, 11, 30), count=2)
    assert (
        service.validate_room_changes(
            two_mondays, [room.id], two_mondays.start_time, two_mondays.end_time
        )
        is None
    )

    four_mondays = _series(host_calendar, _at(3, 11), _at(3, 11, 30))
    with pytest.raises(RoomUnavailableError):
        service.validate_room_changes(
            four_mondays, [room.id], four_mondays.start_time, four_mondays.end_time
        )


def test_a_room_held_by_a_moved_occurrence_of_its_own_is_taken(service, room, host_calendar):
    """The series holds no room; one occurrence, moved, is given a room of its own."""
    series = _series(host_calendar, _at(3, 10), _at(3, 10, 30))
    moved = service.reschedule_event_occurrence(
        series.calendar_fk_id, series.id, _at(10, 10), _at(10, 15), _at(10, 16), "UTC"
    )
    service.update_event(
        moved.calendar_fk_id,
        moved.id,
        CalendarEventInputData(
            title=None,
            description=None,
            start_time=moved.start_time,
            end_time=moved.end_time,
            timezone="UTC",
            resource_allocations=[ResourceAllocationInputData(resource_id=room.id)],
        ),
    )

    with pytest.raises(RoomUnavailableError):
        service.validate_requested_rooms([room.id], _at(10, 15, 30), _at(10, 16))


def test_adding_a_room_free_on_every_occurrence_is_allowed(service, room, host_calendar):
    series = _series(host_calendar, _at(3, 10), _at(3, 10, 30))
    _taken_by_an_event(room, host_calendar)

    assert (
        service.validate_room_changes(series, [room.id], series.start_time, series.end_time) is None
    )


def test_a_cancelled_occurrence_does_not_need_the_room(service, organization, room, host_calendar):
    series = _series(host_calendar, _at(3, 11), _at(3, 11, 30))
    EventRecurrenceException.objects.create(
        organization=organization,
        parent_event_fk=series,
        exception_date=_at(17, 11),
        is_cancelled=True,
    )
    _taken_by_an_event(room, host_calendar)

    assert (
        service.validate_room_changes(series, [room.id], series.start_time, series.end_time) is None
    )


def test_occurrences_already_over_are_not_checked(service, room, host_calendar):
    start = datetime.datetime(2020, 6, 1, 11, 0, tzinfo=datetime.UTC)
    series = _series(host_calendar, start, start + datetime.timedelta(minutes=30))
    _event_holding(
        room,
        host_calendar,
        start + datetime.timedelta(weeks=2),
        start + datetime.timedelta(weeks=2, hours=1),
    )

    assert (
        service.validate_room_changes(series, [room.id], series.start_time, series.end_time) is None
    )


@pytest.mark.parametrize(
    ("weeks_ahead", "refused"),
    [(51, True), (53, False)],
    ids=["inside-the-lookahead", "past-the-lookahead"],
)
def test_an_endless_series_is_checked_up_to_the_lookahead(
    service, room, host_calendar, weeks_ahead, refused
):
    series = _series(host_calendar, _at(3, 11), _at(3, 11, 30), count=None)
    _event_holding(
        room,
        host_calendar,
        _at(3, 11) + datetime.timedelta(weeks=weeks_ahead),
        _at(3, 12) + datetime.timedelta(weeks=weeks_ahead),
    )

    def check():
        service.validate_room_changes(series, [room.id], series.start_time, series.end_time)

    if refused:
        with pytest.raises(RoomUnavailableError):
            check()
    else:
        check()
