"""Times written by recurring exceptions, for events, blocked times and available times.

An exception on the master's own date turns the master into a one-off object and
starts a new series on the second occurrence. The modified times must land on the
master, and the new series must keep the original local time.

``start_time`` / ``end_time`` are generated columns derived from the
``*_tz_unaware`` wall-clock plus ``timezone``, so writes must go to the wall-clock
fields. Event services take UTC instants. Blocked-time and available-time writers
take the local wall-clock. Every test uses America/Sao_Paulo (UTC-3), where mixing
the two shows up as a three-hour shift.
"""

import datetime
from unittest import mock

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOwnership,
)
from calendar_integration.services.availability_service import AvailabilityService
from calendar_integration.services.calendar_event_service import CalendarEventService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    EventAttendanceInputData,
    EventExternalAttendanceInputData,
    ExternalAttendeeInputData,
)
from organizations.models import Organization, OrganizationMembership
from users.models import Profile, User


TZ = "America/Sao_Paulo"
DAY_ONE = datetime.date(2030, 6, 3)


def _utc(day: int, hour: int) -> datetime.datetime:
    return datetime.datetime(2030, 6, day, hour, 0, tzinfo=datetime.UTC)


def _wall_clock(day: int, hour: int) -> datetime.datetime:
    """A local wall-clock time, as the blocked/available writers take it."""
    return datetime.datetime(2030, 6, day, hour, 0)


def _wall_clock_of(obj: CalendarEvent | BlockedTime | AvailableTime) -> tuple:
    """The stored local wall-clock of a row, as naive datetimes."""
    obj.refresh_from_db()
    return (
        obj.start_time_tz_unaware.replace(tzinfo=None),
        obj.end_time_tz_unaware.replace(tzinfo=None),
    )


def _times(obj: CalendarEvent | BlockedTime | AvailableTime) -> tuple:
    obj.refresh_from_db()
    return (obj.start_time, obj.end_time, obj.timezone, obj.recurrence_rule_fk_id is None)


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization)


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    calendar = baker.make(Calendar, organization=organization, provider=CalendarProvider.INTERNAL)
    # The event tests rely on this being off: the availability check must not apply.
    assert calendar.manage_available_windows is False
    return calendar


@pytest.fixture
def service(organization: Organization) -> CalendarService:
    calendar_service = CalendarService()
    calendar_service.initialize_without_provider(organization=organization)
    return calendar_service


@pytest.fixture
def owner_service(organization: Organization, calendar: Calendar) -> CalendarService:
    """A service acting as the calendar's owner, which event writes require."""
    owner = User.objects.create_user(email=f"owner-{organization.pk}@example.com", password="x")
    Profile.objects.create(user=owner)
    OrganizationMembership.objects.create(user=owner, organization=organization, is_active=True)
    CalendarOwnership.objects.create(
        calendar=calendar, membership_user_id=owner.id, organization=organization
    )
    CalendarPermissionService().create_calendar_owner_token(
        organization_id=organization.id, user=owner, calendar_id=calendar.id
    )
    calendar_service = CalendarService()
    calendar_service.initialize_without_provider(owner, organization)
    return calendar_service


def _other_rows(model, organization: Organization, *exclude: int) -> list:
    return list(
        model.original_manager.filter(organization=organization)
        .exclude(pk__in=exclude)
        .order_by("start_time")
    )


# ---------------------------------------------------------------------------
# Events: the service takes UTC instants.
# ---------------------------------------------------------------------------


@pytest.fixture
def daily_event(owner_service: CalendarService, calendar: Calendar) -> CalendarEvent:
    """A daily 10:00-11:00 Sao Paulo event (13:00Z-14:00Z), five occurrences."""
    return owner_service.create_recurring_event(
        calendar.id,
        title="Daily",
        description="",
        start_time=_utc(3, 13),
        end_time=_utc(3, 14),
        timezone=TZ,
        recurrence_rule="FREQ=DAILY;COUNT=5",
    )


@pytest.mark.django_db
def test_event_master_date_modification_moves_the_master(
    owner_service: CalendarService, organization: Organization, daily_event: CalendarEvent
):
    result = owner_service.create_recurring_event_exception(
        parent_event=daily_event,
        exception_date=DAY_ONE,
        modified_title="Moved",
        modified_start_time=_utc(3, 17),
        modified_end_time=_utc(3, 18),
    )

    assert result is not None
    assert result.pk == daily_event.pk
    assert result.title == "Moved"
    assert _times(daily_event) == (_utc(3, 17), _utc(3, 18), TZ, True)

    [new_series] = _other_rows(CalendarEvent, organization, daily_event.pk)
    assert new_series.title == "Daily"
    assert _times(new_series) == (_utc(4, 13), _utc(4, 14), TZ, False)


@pytest.mark.django_db
def test_event_master_date_timezone_change_keeps_the_instant(
    owner_service: CalendarService, organization: Organization, daily_event: CalendarEvent
):
    owner_service.create_recurring_event_exception(
        parent_event=daily_event,
        exception_date=DAY_ONE,
        modified_timezone="Asia/Tokyo",
    )

    assert _times(daily_event) == (_utc(3, 13), _utc(3, 14), "Asia/Tokyo", True)

    [new_series] = _other_rows(CalendarEvent, organization, daily_event.pk)
    assert _times(new_series) == (_utc(4, 13), _utc(4, 14), TZ, False)


@pytest.mark.django_db
def test_event_master_date_title_only_leaves_the_times_alone(
    owner_service: CalendarService, daily_event: CalendarEvent
):
    before = _wall_clock_of(daily_event)

    owner_service.create_recurring_event_exception(
        parent_event=daily_event, exception_date=DAY_ONE, modified_title="Renamed"
    )

    assert _wall_clock_of(daily_event) == before
    assert _times(daily_event) == (_utc(3, 13), _utc(3, 14), TZ, True)
    assert daily_event.title == "Renamed"


@pytest.mark.django_db
def test_event_master_date_cancel_removes_only_the_first_occurrence(
    owner_service: CalendarService,
    calendar: Calendar,
    organization: Organization,
    daily_event: CalendarEvent,
):
    result = owner_service.create_recurring_event_exception(
        parent_event=daily_event, exception_date=DAY_ONE, is_cancelled=True
    )

    assert result is None
    # The series is kept as it was: same times, still recurring, no new series.
    assert _times(daily_event) == (_utc(3, 13), _utc(3, 14), TZ, False)
    assert _other_rows(CalendarEvent, organization, daily_event.pk) == []
    occurrences = owner_service.get_calendar_events_expanded(
        calendar=calendar, start_date=_utc(3, 0), end_date=_utc(6, 0)
    )
    assert sorted(o.start_time for o in occurrences) == [_utc(4, 13), _utc(5, 13)]


@pytest.mark.django_db
def test_event_master_date_new_start_alone_keeps_the_duration(
    owner_service: CalendarService, daily_event: CalendarEvent
):
    owner_service.create_recurring_event_exception(
        parent_event=daily_event, exception_date=DAY_ONE, modified_start_time=_utc(3, 17)
    )

    assert _times(daily_event) == (_utc(3, 17), _utc(3, 18), TZ, True)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("start", "end"),
    [
        (None, _utc(3, 12)),  # End alone, before the current start.
        (None, _utc(3, 13)),  # End alone, equal to the current start.
        (_utc(3, 17), _utc(3, 16)),  # Both given, reversed.
        (_utc(3, 17), _utc(3, 17)),  # Both given, empty.
    ],
)
def test_event_master_date_rejects_an_end_that_is_not_after_the_start(
    owner_service: CalendarService,
    daily_event: CalendarEvent,
    start: datetime.datetime | None,
    end: datetime.datetime,
):
    with pytest.raises(ValueError, match="end_time must be after start_time"):
        owner_service.create_recurring_event_exception(
            parent_event=daily_event,
            exception_date=DAY_ONE,
            modified_start_time=start,
            modified_end_time=end,
        )

    # The failed edit leaves the whole series as it was.
    assert _times(daily_event) == (_utc(3, 13), _utc(3, 14), TZ, False)


@pytest.mark.django_db
def test_event_master_date_without_a_second_occurrence_becomes_a_one_off(
    owner_service: CalendarService, calendar: Calendar, organization: Organization
):
    # The rule ends on the first day, so there is no second occurrence.
    event = owner_service.create_recurring_event(
        calendar.id,
        title="Once",
        description="",
        start_time=_utc(3, 13),
        end_time=_utc(3, 14),
        timezone=TZ,
        recurrence_rule="FREQ=DAILY;UNTIL=20300603T235959Z",
    )

    owner_service.create_recurring_event_exception(
        parent_event=event,
        exception_date=DAY_ONE,
        modified_title="Moved",
        modified_start_time=_utc(3, 17),
        modified_end_time=_utc(3, 18),
    )

    assert _times(event) == (_utc(3, 17), _utc(3, 18), TZ, True)
    assert event.title == "Moved"
    assert _other_rows(CalendarEvent, organization, event.pk) == []


@pytest.mark.django_db
def test_event_master_date_keeps_the_master_attendances(
    owner_service: CalendarService, calendar: Calendar, organization: Organization
):
    attendee = User.objects.create_user(
        email=f"attendee-{organization.pk}@example.com", password="x"
    )
    Profile.objects.create(user=attendee)
    OrganizationMembership.objects.create(user=attendee, organization=organization, is_active=True)
    event = owner_service.create_recurring_event(
        calendar.id,
        title="With guests",
        description="",
        start_time=_utc(3, 13),
        end_time=_utc(3, 14),
        timezone=TZ,
        recurrence_rule="FREQ=DAILY;COUNT=5",
        attendances=[EventAttendanceInputData(user_id=attendee.id)],
        external_attendances=[
            EventExternalAttendanceInputData(
                external_attendee=ExternalAttendeeInputData(email="guest@example.com", name="Guest")
            )
        ],
    )
    attendance_ids = set(event.attendances.values_list("pk", flat=True))
    external_attendance_ids = set(event.external_attendances.values_list("pk", flat=True))
    assert len(attendance_ids) == 1
    assert len(external_attendance_ids) == 1

    owner_service.create_recurring_event_exception(
        parent_event=event, exception_date=DAY_ONE, modified_title="Renamed"
    )

    # The same rows, not re-created copies, still hang off the master.
    event.refresh_from_db()
    assert set(event.attendances.values_list("pk", flat=True)) == attendance_ids
    assert set(event.external_attendances.values_list("pk", flat=True)) == external_attendance_ids


@pytest.mark.django_db
def test_event_master_date_failure_keeps_the_series(
    owner_service: CalendarService, daily_event: CalendarEvent
):
    rule_id = daily_event.recurrence_rule_fk_id
    assert rule_id is not None

    with (
        mock.patch.object(
            CalendarEventService, "create_recurring_event", side_effect=RuntimeError("boom")
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        owner_service.create_recurring_event_exception(
            parent_event=daily_event, exception_date=DAY_ONE, modified_title="Renamed"
        )

    daily_event.refresh_from_db()
    assert daily_event.recurrence_rule_fk_id == rule_id
    assert daily_event.recurrence_rule is not None
    assert daily_event.title == "Daily"


# ---------------------------------------------------------------------------
# Blocked times and available times: the writers take the local wall-clock.
# ---------------------------------------------------------------------------


def _create_daily(kind: str, service: CalendarService, calendar: Calendar):
    """A daily 10:00-11:00 Sao Paulo row (13:00Z-14:00Z), five occurrences."""
    if kind == "blocked":
        return service.create_blocked_time(
            calendar=calendar,
            start_time=_wall_clock(3, 10),
            end_time=_wall_clock(3, 11),
            timezone=TZ,
            rrule_string="FREQ=DAILY;COUNT=5",
        )
    calendar.manage_available_windows = True
    calendar.save(update_fields=["manage_available_windows"])
    return service.create_available_time(
        calendar=calendar,
        start_time=_wall_clock(3, 10),
        end_time=_wall_clock(3, 11),
        timezone=TZ,
        rrule_string="FREQ=DAILY;COUNT=5",
    )


def _create_exception(kind: str, service: CalendarService, parent, **kwargs):
    if kind == "blocked":
        return service.create_recurring_blocked_time_exception(parent_blocked_time=parent, **kwargs)
    return service.create_recurring_available_time_exception(parent_available_time=parent, **kwargs)


def _model(kind: str):
    return BlockedTime if kind == "blocked" else AvailableTime


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["blocked", "available"])
def test_master_date_modification_moves_the_master(
    kind: str, service: CalendarService, calendar: Calendar, organization: Organization
):
    parent = _create_daily(kind, service, calendar)

    result = _create_exception(
        kind,
        service,
        parent,
        exception_date=DAY_ONE,
        modified_start_time=_wall_clock(3, 14),
        modified_end_time=_wall_clock(3, 15),
    )

    assert result is not None
    assert result.pk == parent.pk
    assert _times(parent) == (_utc(3, 17), _utc(3, 18), TZ, True)

    [new_series] = _other_rows(_model(kind), organization, parent.pk)
    assert _times(new_series) == (_utc(4, 13), _utc(4, 14), TZ, False)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["blocked", "available"])
def test_later_date_modification_without_new_times_keeps_the_local_time(
    kind: str, service: CalendarService, calendar: Calendar
):
    parent = _create_daily(kind, service, calendar)

    result = _create_exception(
        kind,
        service,
        parent,
        exception_date=datetime.date(2030, 6, 5),
        modified_timezone=TZ,
    )

    assert result is not None
    assert _times(result) == (_utc(5, 13), _utc(5, 14), TZ, True)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["blocked", "available"])
def test_later_date_modification_with_new_times(
    kind: str, service: CalendarService, calendar: Calendar
):
    parent = _create_daily(kind, service, calendar)

    result = _create_exception(
        kind,
        service,
        parent,
        exception_date=datetime.date(2030, 6, 5),
        modified_start_time=_wall_clock(5, 14),
        modified_end_time=_wall_clock(5, 15),
    )

    assert result is not None
    assert _times(result) == (_utc(5, 17), _utc(5, 18), TZ, True)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["blocked", "available"])
def test_master_date_timezone_change_keeps_the_local_time(
    kind: str, service: CalendarService, calendar: Calendar, organization: Organization
):
    parent = _create_daily(kind, service, calendar)

    _create_exception(kind, service, parent, exception_date=DAY_ONE, modified_timezone="Asia/Tokyo")

    # 10:00 stays 10:00 locally, so the instant moves: 10:00 in Tokyo (UTC+9) is 01:00Z.
    assert _times(parent) == (_utc(3, 1), _utc(3, 2), "Asia/Tokyo", True)

    [new_series] = _other_rows(_model(kind), organization, parent.pk)
    assert _times(new_series) == (_utc(4, 13), _utc(4, 14), TZ, False)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["blocked", "available"])
def test_master_date_new_start_alone_keeps_the_duration(
    kind: str, service: CalendarService, calendar: Calendar
):
    parent = _create_daily(kind, service, calendar)

    _create_exception(
        kind, service, parent, exception_date=DAY_ONE, modified_start_time=_wall_clock(3, 14)
    )

    assert _times(parent) == (_utc(3, 17), _utc(3, 18), TZ, True)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["blocked", "available"])
@pytest.mark.parametrize(
    ("start", "end"),
    [
        (None, _wall_clock(3, 9)),  # End alone, before the current start.
        (None, _wall_clock(3, 10)),  # End alone, equal to the current start.
        (_wall_clock(3, 14), _wall_clock(3, 13)),  # Both given, reversed.
        (_wall_clock(3, 14), _wall_clock(3, 14)),  # Both given, empty.
    ],
)
def test_master_date_rejects_an_end_that_is_not_after_the_start(
    kind: str,
    service: CalendarService,
    calendar: Calendar,
    start: datetime.datetime | None,
    end: datetime.datetime,
):
    parent = _create_daily(kind, service, calendar)

    with pytest.raises(ValueError, match="end_time must be after start_time"):
        _create_exception(
            kind,
            service,
            parent,
            exception_date=DAY_ONE,
            modified_start_time=start,
            modified_end_time=end,
        )

    # The failed edit leaves the whole series as it was.
    assert _times(parent) == (_utc(3, 13), _utc(3, 14), TZ, False)


@pytest.mark.django_db
def test_blocked_master_date_reason_only_leaves_the_times_alone(
    service: CalendarService, calendar: Calendar
):
    parent = _create_daily("blocked", service, calendar)

    service.create_recurring_blocked_time_exception(
        parent_blocked_time=parent, exception_date=DAY_ONE, modified_reason="Dentist"
    )

    assert _times(parent) == (_utc(3, 13), _utc(3, 14), TZ, True)
    assert parent.reason == "Dentist"


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["blocked", "available"])
def test_master_date_without_a_second_occurrence_becomes_a_one_off(
    kind: str, service: CalendarService, calendar: Calendar, organization: Organization
):
    # The rule ends on the first day, so there is no second occurrence.
    parent = _create_daily(kind, service, calendar)
    rule = parent.recurrence_rule
    rule.count = None
    rule.until = _utc(3, 23)
    rule.save()

    result = _create_exception(
        kind,
        service,
        parent,
        exception_date=DAY_ONE,
        modified_start_time=_wall_clock(3, 14),
        modified_end_time=_wall_clock(3, 15),
    )

    assert result is not None
    assert _times(parent) == (_utc(3, 17), _utc(3, 18), TZ, True)
    assert _other_rows(_model(kind), organization, parent.pk) == []


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["blocked", "available"])
def test_master_date_failure_keeps_the_series(
    kind: str, service: CalendarService, calendar: Calendar
):
    parent = _create_daily(kind, service, calendar)
    rule_id = parent.recurrence_rule_fk_id
    assert rule_id is not None
    method = "create_blocked_time" if kind == "blocked" else "create_available_time"

    with (
        mock.patch.object(AvailabilityService, method, side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        _create_exception(kind, service, parent, exception_date=DAY_ONE)

    parent.refresh_from_db()
    assert parent.recurrence_rule_fk_id == rule_id
    assert parent.recurrence_rule is not None


# ---------------------------------------------------------------------------
# Bulk modification: the continuation series is written as local wall-clock.
# ---------------------------------------------------------------------------


def _bulk_modify(kind: str, service: CalendarService, parent, **kwargs):
    if kind == "blocked":
        return service.create_recurring_blocked_time_bulk_modification(
            parent_blocked_time=parent, **kwargs
        )
    return service.create_recurring_available_time_bulk_modification(
        parent_available_time=parent, **kwargs
    )


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["blocked", "available"])
@pytest.mark.parametrize(
    ("start_offset", "expected_start", "expected_end"),
    [
        (None, _utc(5, 13), _utc(5, 14)),
        (datetime.timedelta(hours=1), _utc(5, 14), _utc(5, 15)),
    ],
)
def test_bulk_modification_continuation_keeps_the_local_time(
    kind: str,
    service: CalendarService,
    calendar: Calendar,
    start_offset: datetime.timedelta | None,
    expected_start: datetime.datetime,
    expected_end: datetime.datetime,
):
    parent = _create_daily(kind, service, calendar)

    continuation = _bulk_modify(
        kind,
        service,
        parent,
        modification_start_date=_utc(5, 13),  # the 5 June occurrence, 10:00 local
        modified_start_time_offset=start_offset,
    )

    assert continuation is not None
    assert _times(continuation) == (expected_start, expected_end, TZ, False)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["blocked", "available"])
def test_master_date_cancel_removes_only_the_first_occurrence(
    kind: str, service: CalendarService, calendar: Calendar, organization: Organization
):
    parent = _create_daily(kind, service, calendar)

    result = _create_exception(kind, service, parent, exception_date=DAY_ONE, is_cancelled=True)

    assert result is None
    assert _times(parent) == (_utc(3, 13), _utc(3, 14), TZ, False)
    assert _other_rows(_model(kind), organization, parent.pk) == []
    expand = (
        service.get_blocked_times_expanded
        if kind == "blocked"
        else service.get_available_times_expanded
    )
    occurrences = expand(calendar=calendar, start_date=_utc(3, 0), end_date=_utc(6, 0))
    assert sorted(o.start_time for o in occurrences) == [_utc(4, 13), _utc(5, 13)]
