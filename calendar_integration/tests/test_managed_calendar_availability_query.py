"""``Calendar.objects.only_calendars_available_in_ranges`` for calendars that manage windows.

A managed calendar is available for a range when an availability window occurrence
covers it -- recurring windows expanded, not only the first occurrence stored on the
master row -- and no event or blocked time overlaps it. Appointment-type booking,
appointment-type slot discovery and calendar-group availability all go through this
query.
"""

import datetime

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.booking_auth import BOOKING_CODE_HEADER
from calendar_integration.models import (
    AppointmentType,
    AppointmentTypeSlot,
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    RecurrenceRule,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.tests.test_event_creation_surfaces import (
    _appointment_type_booking_code,
    _appointment_type_with_one_slot,
)
from calendar_integration.tests.test_event_wall_clock_input import END, START, TZ, _seed_window
from organizations.models import Organization


DAY_ONE = datetime.date(2030, 6, 3)
LATER_DAY = datetime.date(2030, 6, 7)


def _utc(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(day.year, day.month, day.day, hour, minute, tzinfo=datetime.UTC)


@pytest.fixture
def organization():
    return baker.make(Organization)


@pytest.fixture
def service(organization):
    calendar_service = CalendarService()
    calendar_service.initialize_without_provider(organization=organization)
    return calendar_service


@pytest.fixture
def calendar(organization):
    return baker.make(Calendar, organization=organization, manage_available_windows=True)


@pytest.fixture
def daily_window(service, calendar):
    """A daily 09:00-17:00 UTC window starting on ``DAY_ONE``."""
    return service.create_available_time(
        calendar=calendar,
        start_time=datetime.datetime(2030, 6, 3, 9, 0),
        end_time=datetime.datetime(2030, 6, 3, 17, 0),
        timezone="UTC",
        rrule_string="FREQ=DAILY",
    )


def _is_available(calendar: Calendar, start: datetime.datetime, end: datetime.datetime) -> bool:
    return (
        Calendar.objects.filter_by_organization(calendar.organization_id)
        .only_calendars_available_in_ranges([(start, end)])
        .filter(id=calendar.id)
        .exists()
    )


def _is_available_with_bulk_modifications(
    calendar: Calendar, start: datetime.datetime, end: datetime.datetime
) -> bool:
    return (
        Calendar.objects.filter_by_organization(calendar.organization_id)
        .only_calendars_available_in_ranges_with_bulk_modifications([(start, end)])
        .filter(id=calendar.id)
        .exists()
    )


@pytest.mark.django_db
@pytest.mark.usefixtures("daily_window")
@pytest.mark.parametrize("day", [DAY_ONE, LATER_DAY])
def test_recurring_window_covers_every_occurrence(calendar, day):
    assert _is_available(calendar, _utc(day, 10), _utc(day, 11))
    assert _is_available_with_bulk_modifications(calendar, _utc(day, 10), _utc(day, 11))


@pytest.mark.django_db
@pytest.mark.usefixtures("daily_window")
def test_range_must_be_fully_covered(calendar):
    assert not _is_available(calendar, _utc(LATER_DAY, 16), _utc(LATER_DAY, 18))
    assert not _is_available(calendar, _utc(LATER_DAY, 18), _utc(LATER_DAY, 19))


@pytest.mark.django_db
def test_recurring_window_in_non_utc_timezone(service, calendar):
    service.create_available_time(
        calendar=calendar,
        start_time=datetime.datetime(2030, 6, 3, 9, 0),
        end_time=datetime.datetime(2030, 6, 3, 17, 0),
        timezone="America/Sao_Paulo",
        rrule_string="FREQ=DAILY",
    )

    # 10:00-11:00 in Sao Paulo (UTC-3).
    assert _is_available(calendar, _utc(LATER_DAY, 13), _utc(LATER_DAY, 14))
    # 07:00-08:00 in Sao Paulo, before the window opens.
    assert not _is_available(calendar, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11))


@pytest.mark.django_db
def test_non_recurring_window(calendar, organization):
    baker.make(
        AvailableTime,
        organization=organization,
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 7, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 7, 17, 0),
        timezone="UTC",
    )

    assert _is_available(calendar, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11))
    assert not _is_available(calendar, _utc(DAY_ONE, 10), _utc(DAY_ONE, 11))


@pytest.mark.django_db
@pytest.mark.usefixtures("daily_window")
def test_overlapping_event_makes_calendar_unavailable(calendar, organization):
    baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 7, 10, 30),
        end_time_tz_unaware=datetime.datetime(2030, 6, 7, 11, 30),
    )

    assert not _is_available(calendar, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11))
    assert not _is_available_with_bulk_modifications(
        calendar, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11)
    )


@pytest.mark.django_db
@pytest.mark.usefixtures("daily_window")
def test_back_to_back_event_does_not_conflict(calendar, organization):
    baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 7, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 7, 10, 0),
    )

    assert _is_available(calendar, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11))


@pytest.mark.django_db
@pytest.mark.usefixtures("daily_window")
def test_recurring_event_occurrence_makes_calendar_unavailable(calendar, organization):
    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY", organization=organization)
    rule.save()
    baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 3, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 3, 11, 0),
        recurrence_rule=rule,
    )

    assert not _is_available(calendar, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11))
    assert _is_available(calendar, _utc(LATER_DAY, 11), _utc(LATER_DAY, 12))


@pytest.mark.django_db
@pytest.mark.usefixtures("daily_window")
def test_blocked_time_makes_calendar_unavailable(service, calendar):
    service.create_blocked_time(
        calendar=calendar,
        start_time=datetime.datetime(2030, 6, 7, 10, 0),
        end_time=datetime.datetime(2030, 6, 7, 12, 0),
        timezone="UTC",
    )

    assert not _is_available(calendar, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11))
    assert _is_available(calendar, _utc(LATER_DAY, 12), _utc(LATER_DAY, 13))


@pytest.mark.django_db
@pytest.mark.usefixtures("daily_window")
def test_recurring_blocked_time_occurrence_makes_calendar_unavailable(service, calendar):
    service.create_blocked_time(
        calendar=calendar,
        start_time=datetime.datetime(2030, 6, 3, 12, 0),
        end_time=datetime.datetime(2030, 6, 3, 13, 0),
        timezone="UTC",
        rrule_string="FREQ=DAILY",
    )

    assert not _is_available(calendar, _utc(LATER_DAY, 12), _utc(LATER_DAY, 13))
    assert _is_available(calendar, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11))


@pytest.mark.django_db
def test_appointment_type_scoped_window_does_not_count(calendar, organization):
    appointment_type = baker.make(AppointmentType, organization=organization)
    slot = AppointmentTypeSlot.objects.create(
        organization=organization,
        appointment_type=appointment_type,
        name="Providers",
        order=0,
        required_count=1,
    )
    AvailableTime.objects.unscoped().create(
        organization=organization,
        calendar=calendar,
        appointment_type_slot=slot,
        start_time_tz_unaware=datetime.datetime(2030, 6, 7, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 7, 17, 0),
        timezone="UTC",
    )

    assert not _is_available(calendar, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11))


@pytest.mark.django_db
@pytest.mark.usefixtures("daily_window")
def test_other_organization_rows_are_ignored(calendar):
    other_organization = baker.make(Organization)
    # A row naming this calendar but another organization must not count.
    baker.make(
        CalendarEvent,
        organization=other_organization,
        calendar_fk_id=calendar.id,
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 7, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 7, 11, 0),
    )

    assert _is_available(calendar, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11))


@pytest.mark.django_db
def test_unmanaged_calendar_with_conflicting_event_is_unavailable(organization):
    unmanaged = baker.make(Calendar, organization=organization, manage_available_windows=False)
    baker.make(
        CalendarEvent,
        organization=organization,
        calendar=unmanaged,
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 7, 10, 30),
        end_time_tz_unaware=datetime.datetime(2030, 6, 7, 11, 30),
    )

    assert not _is_available(unmanaged, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11))
    assert _is_available(unmanaged, _utc(LATER_DAY, 12), _utc(LATER_DAY, 13))


@pytest.mark.django_db
@pytest.mark.usefixtures("daily_window")
def test_appointment_type_scoped_blocked_time_is_ignored(calendar, organization):
    appointment_type = baker.make(AppointmentType, organization=organization)
    slot = AppointmentTypeSlot.objects.create(
        organization=organization,
        appointment_type=appointment_type,
        name="Providers",
        order=0,
        required_count=1,
    )
    # Slot-scoped blocks are checked per slot elsewhere, so they must not block here.
    BlockedTime.objects.unscoped().create(
        organization=organization,
        calendar=calendar,
        appointment_type_slot=slot,
        start_time_tz_unaware=datetime.datetime(2030, 6, 7, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 7, 11, 0),
        timezone="UTC",
        reason="Slot scoped",
    )

    assert _is_available(calendar, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11))


@pytest.mark.django_db
@pytest.mark.usefixtures("daily_window")
def test_every_range_must_be_available(calendar, organization):
    baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 7, 10, 30),
        end_time_tz_unaware=datetime.datetime(2030, 6, 7, 11, 30),
    )
    free = (_utc(LATER_DAY, 12), _utc(LATER_DAY, 13))
    also_free = (_utc(LATER_DAY, 14), _utc(LATER_DAY, 15))
    taken = (_utc(LATER_DAY, 10), _utc(LATER_DAY, 11))

    def available_for(ranges: list[tuple[datetime.datetime, datetime.datetime]]) -> bool:
        return (
            Calendar.objects.filter_by_organization(calendar.organization_id)
            .only_calendars_available_in_ranges(ranges)
            .filter(id=calendar.id)
            .exists()
        )

    assert available_for([free, also_free])
    assert not available_for([free, taken])


@pytest.mark.django_db
@pytest.mark.usefixtures("daily_window")
def test_flagged_instance_row_without_exception_row_still_blocks(calendar, organization):
    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY", organization=organization)
    rule.save()
    master = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 3, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 3, 11, 0),
        recurrence_rule=rule,
        external_id="series-master",
    )
    # The calendar sync creates instance rows like this one: flagged as an exception,
    # tied to the series, but with no row in the recurrence exception table.
    baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 7, 14, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 7, 15, 0),
        parent_recurring_object=master,
        is_recurring_exception=True,
        external_id="series-instance",
    )

    assert not _is_available(calendar, _utc(LATER_DAY, 14), _utc(LATER_DAY, 15))
    assert _is_available(calendar, _utc(LATER_DAY, 12), _utc(LATER_DAY, 13))


@pytest.mark.django_db
@pytest.mark.usefixtures("daily_window")
def test_event_moved_by_an_exception_blocks_only_its_new_time(calendar, organization):
    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY", organization=organization)
    rule.save()
    master = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 3, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 3, 11, 0),
        recurrence_rule=rule,
        external_id="series-master",
    )
    moved = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 7, 14, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 7, 15, 0),
        parent_recurring_object=master,
        is_recurring_exception=True,
        external_id="series-instance",
    )
    master.create_exception(_utc(LATER_DAY, 10), is_cancelled=False, modified_object=moved)

    assert not _is_available(calendar, _utc(LATER_DAY, 14), _utc(LATER_DAY, 15))
    assert _is_available(calendar, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11))


@pytest.mark.django_db
def test_window_series_that_ended_does_not_make_calendar_available(service, calendar):
    service.create_available_time(
        calendar=calendar,
        start_time=datetime.datetime(2030, 6, 3, 9, 0),
        end_time=datetime.datetime(2030, 6, 3, 17, 0),
        timezone="UTC",
        rrule_string="FREQ=DAILY;UNTIL=20300605T090000Z",
    )

    assert _is_available(calendar, _utc(DAY_ONE, 10), _utc(DAY_ONE, 11))
    assert _is_available(
        calendar, _utc(datetime.date(2030, 6, 5), 10), _utc(DAY_ONE.replace(day=5), 11)
    )
    assert not _is_available(calendar, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11))
    assert not _is_available_with_bulk_modifications(
        calendar, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11)
    )


@pytest.mark.django_db
def test_cancelled_window_occurrence_makes_that_day_unavailable(daily_window, calendar):
    daily_window.create_exception(_utc(LATER_DAY, 9), is_cancelled=True)

    assert not _is_available(calendar, _utc(LATER_DAY, 10), _utc(LATER_DAY, 11))
    assert _is_available(calendar, _utc(DAY_ONE, 10), _utc(DAY_ONE, 11))
    next_day = LATER_DAY + datetime.timedelta(days=1)
    assert _is_available(calendar, _utc(next_day, 10), _utc(next_day, 11))


@pytest.mark.django_db
def test_booking_is_rejected_when_an_event_already_holds_the_slot():
    organization = baker.make(Organization)
    appointment_type, slot, calendar = _appointment_type_with_one_slot(organization)
    _seed_window(calendar)
    # 10:00-11:00 in Sao Paulo on the booked day, already taken.
    baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        timezone=TZ,
        start_time_tz_unaware=datetime.datetime(2030, 6, 5, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 5, 11, 0),
        external_id="already-booked",
    )
    _token, code = _appointment_type_booking_code(organization, appointment_type)

    response = APIClient().post(
        reverse(
            "calendar_booking_api:booking-appointment-type-events-list",
            kwargs={"public_slug": appointment_type.public_booking_slug},
        ),
        {
            "title": "Second booking",
            "start_time": START,
            "end_time": END,
            "timezone": TZ,
            "slot_selections": [{"slot_id": slot.id, "calendar_ids": [calendar.id]}],
            "external_attendee": {"email": "patient@example.com", "name": "Pat"},
        },
        format="json",
        headers={BOOKING_CODE_HEADER: code},
    )

    assert response.status_code == status.HTTP_409_CONFLICT, response.content
    assert response.json()["error_code"] == "SLOT_UNAVAILABLE"
    assert CalendarEvent.original_manager.filter(calendar_fk=calendar).count() == 1
