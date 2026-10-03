"""Recurring series keep their local wall-clock time across DST changes.

A recurring row stores its first occurrence as a local wall-clock plus an IANA
timezone. Every later occurrence must start at that same local time, so its UTC
instant moves by an hour when the zone changes between standard and daylight time.
The local weekday and date also come from the series' own zone, not from UTC.

America/New_York in 2030: daylight time starts on 10 March and ends on 3 November.
09:00 local is 14:00Z in standard time and 13:00Z in daylight time.
"""

import datetime
import zoneinfo
from collections.abc import Callable, Iterable, Sequence
from typing import Any
from unittest.mock import patch

from django.db import DatabaseError, connection, transaction

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider
from calendar_integration.local_time import local_wall_clock_to_utc
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    BlockedTimeRecurrenceException,
    Calendar,
    CalendarEvent,
    RecurrenceRule,
    RecurringMixin,
)
from calendar_integration.recurrence_utils import OccurrenceValidator, RecurrenceRuleSplitter
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.calendar_service_utils import wall_clock_to_utc
from organizations.models import Organization


NEW_YORK = "America/New_York"
SAO_PAULO = "America/Sao_Paulo"


def _utc(month: int, day: int, hour: int) -> datetime.datetime:
    return datetime.datetime(2030, month, day, hour, 0, tzinfo=datetime.UTC)


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization)


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        provider=CalendarProvider.INTERNAL,
        manage_available_windows=True,
    )


@pytest.fixture
def service(organization: Organization) -> CalendarService:
    calendar_service = CalendarService()
    calendar_service.initialize_without_provider(organization=organization)
    return calendar_service


def _new_york_daily_blocked_time(service: CalendarService, calendar: Calendar) -> BlockedTime:
    """Daily 09:00-10:00 New York, starting 1 March 2030 (standard time)."""
    return service.create_blocked_time(
        calendar=calendar,
        start_time=datetime.datetime(2030, 3, 1, 9, 0),
        end_time=datetime.datetime(2030, 3, 1, 10, 0),
        timezone=NEW_YORK,
        rrule_string="FREQ=DAILY",
    )


def _starts(rows: Iterable[RecurringMixin]) -> list[datetime.datetime]:
    return sorted(row.start_time for row in rows)


Expand = Callable[
    [CalendarService, Calendar, datetime.datetime, datetime.datetime], Sequence[RecurringMixin]
]


def _expand_blocked(
    service: CalendarService,
    calendar: Calendar,
    start: datetime.datetime,
    end: datetime.datetime,
) -> list[BlockedTime]:
    return service.get_blocked_times_expanded(calendar=calendar, start_date=start, end_date=end)


def _expand_available(
    service: CalendarService,
    calendar: Calendar,
    start: datetime.datetime,
    end: datetime.datetime,
) -> list[AvailableTime]:
    return service.get_available_times_expanded(calendar=calendar, start_date=start, end_date=end)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["blocked", "available"])
def test_daily_series_keeps_local_time_across_both_dst_changes(
    kind: str, service: CalendarService, calendar: Calendar
):
    create = service.create_blocked_time if kind == "blocked" else service.create_available_time
    expand: Expand = _expand_blocked if kind == "blocked" else _expand_available
    create(
        calendar=calendar,
        start_time=datetime.datetime(2030, 3, 1, 9, 0),
        end_time=datetime.datetime(2030, 3, 1, 10, 0),
        timezone=NEW_YORK,
        rrule_string="FREQ=DAILY",
    )

    assert _starts(expand(service, calendar, _utc(3, 9, 0), _utc(3, 12, 0))) == [
        _utc(3, 9, 14),  # Saturday, standard time
        _utc(3, 10, 13),  # Sunday, daylight time from 02:00 local
        _utc(3, 11, 13),
    ]
    assert _starts(expand(service, calendar, _utc(11, 2, 0), _utc(11, 5, 0))) == [
        _utc(11, 2, 13),  # daylight time
        _utc(11, 3, 14),  # standard time from 02:00 local
        _utc(11, 4, 14),
    ]


@pytest.mark.django_db
def test_recurring_event_keeps_local_time_across_dst(
    calendar: Calendar, organization: Organization
):
    rule = RecurrenceRule.from_rrule_string("FREQ=WEEKLY;BYDAY=MO", organization=organization)
    rule.save()
    event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        timezone=NEW_YORK,
        start_time_tz_unaware=datetime.datetime(2030, 3, 4, 9, 0),  # Monday
        end_time_tz_unaware=datetime.datetime(2030, 3, 4, 10, 0),
        recurrence_rule=rule,
    )

    occurrences = event.get_occurrences_in_range(_utc(3, 1, 0), _utc(3, 20, 0))

    assert _starts(occurrences) == [_utc(3, 4, 14), _utc(3, 11, 13), _utc(3, 18, 13)]


@pytest.mark.django_db
def test_weekday_rule_uses_the_local_weekday(service: CalendarService, calendar: Calendar):
    # 21:00 in Sao Paulo (UTC-3) is 00:00Z on the next day, so a UTC weekday check
    # would put Monday's occurrence on Tuesday.
    service.create_blocked_time(
        calendar=calendar,
        start_time=datetime.datetime(2030, 6, 3, 21, 0),  # Monday
        end_time=datetime.datetime(2030, 6, 3, 22, 0),
        timezone=SAO_PAULO,
        rrule_string="FREQ=WEEKLY;BYDAY=MO,WE",
    )

    occurrences = _expand_blocked(service, calendar, _utc(6, 3, 0), _utc(6, 13, 0))

    assert _starts(occurrences) == [
        _utc(6, 4, 0),  # Monday 3 June, 21:00 local
        _utc(6, 6, 0),  # Wednesday 5 June
        _utc(6, 11, 0),  # Monday 10 June
        _utc(6, 13, 0),  # Wednesday 12 June
    ]


@pytest.mark.django_db
def test_cancelling_an_occurrence_after_dst_change(service: CalendarService, calendar: Calendar):
    parent = _new_york_daily_blocked_time(service, calendar)

    service.create_recurring_blocked_time_exception(
        parent_blocked_time=parent,
        exception_date=datetime.date(2030, 3, 12),
        is_cancelled=True,
    )

    occurrences = _expand_blocked(service, calendar, _utc(3, 11, 0), _utc(3, 14, 0))
    assert _starts(occurrences) == [_utc(3, 11, 13), _utc(3, 13, 13)]


@pytest.mark.django_db
def test_exception_date_is_the_local_date(service: CalendarService, calendar: Calendar):
    # Daily 21:00 Sao Paulo: the 5 June occurrence starts at 00:00Z on 6 June.
    parent = service.create_blocked_time(
        calendar=calendar,
        start_time=datetime.datetime(2030, 6, 3, 21, 0),
        end_time=datetime.datetime(2030, 6, 3, 22, 0),
        timezone=SAO_PAULO,
        rrule_string="FREQ=DAILY",
    )

    service.create_recurring_blocked_time_exception(
        parent_blocked_time=parent,
        exception_date=datetime.date(2030, 6, 5),
        is_cancelled=True,
    )

    occurrences = _expand_blocked(service, calendar, _utc(6, 5, 0), _utc(6, 7, 0))
    assert _starts(occurrences) == [_utc(6, 5, 0), _utc(6, 7, 0)]


@pytest.mark.django_db
def test_bulk_cancel_from_an_occurrence_after_dst_change(
    service: CalendarService, calendar: Calendar
):
    parent = _new_york_daily_blocked_time(service, calendar)

    service.create_recurring_blocked_time_bulk_modification(
        parent_blocked_time=parent,
        modification_start_date=_utc(3, 12, 13),  # 09:00 local, daylight time
        is_bulk_cancelled=True,
    )

    occurrences = _expand_blocked(service, calendar, _utc(3, 10, 0), _utc(3, 15, 0))
    assert _starts(occurrences) == [_utc(3, 10, 13), _utc(3, 11, 13)]


def test_occurrence_start_on_uses_local_time_and_date():
    blocked_time = BlockedTime(
        timezone=NEW_YORK,
        start_time_tz_unaware=datetime.datetime(2030, 3, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 3, 1, 10, 0),
    )

    assert blocked_time.occurrence_start_on(datetime.date(2030, 3, 12)) == _utc(3, 12, 13)
    assert blocked_time.occurrence_start_on(datetime.date(2030, 3, 1)) == _utc(3, 1, 14)


@pytest.mark.django_db
def test_expansion_does_not_change_the_callers_timezone(
    service: CalendarService, calendar: Calendar
):
    # The functions switch the session timezone while they run. pytest-django keeps
    # the whole test in one transaction, so a leak would show up here.
    _new_york_daily_blocked_time(service, calendar)
    _expand_blocked(service, calendar, _utc(3, 9, 0), _utc(3, 12, 0))

    with connection.cursor() as cursor:
        cursor.execute("SHOW TIME ZONE")
        assert cursor.fetchone() == ("UTC",)


def _daily_blocked_time(
    service: CalendarService,
    calendar: Calendar,
    start: datetime.datetime,
    end: datetime.datetime,
    timezone: str = NEW_YORK,
    rrule_string: str = "FREQ=DAILY",
) -> BlockedTime:
    return service.create_blocked_time(
        calendar=calendar,
        start_time=start,
        end_time=end,
        timezone=timezone,
        rrule_string=rrule_string,
    )


def _utc_at(month: int, day: int, hour: int, minute: int) -> datetime.datetime:
    return datetime.datetime(2030, month, day, hour, minute, tzinfo=datetime.UTC)


@pytest.mark.django_db
def test_multi_day_occurrences_last_the_same_absolute_time_across_dst(
    service: CalendarService, calendar: Calendar
):
    # Friday 09:00 to Sunday 10:00 New York is 48 hours, because the clocks go
    # forward at 02:00 on Sunday 10 March. A duration kept as calendar days would
    # make every occurrence one hour short.
    _daily_blocked_time(
        service,
        calendar,
        start=datetime.datetime(2030, 3, 8, 9, 0),
        end=datetime.datetime(2030, 3, 10, 10, 0),
        rrule_string="FREQ=WEEKLY",
    )

    occurrences = sorted(
        _expand_blocked(service, calendar, _utc(3, 8, 0), _utc(3, 17, 0)),
        key=lambda occurrence: occurrence.start_time,
    )

    assert [occurrence.start_time for occurrence in occurrences] == [
        _utc(3, 8, 14),
        _utc(3, 15, 13),
    ]
    # The first occurrence is the series' own row, so its end is the stored end.
    assert occurrences[0].end_time == _utc(3, 10, 14)
    for occurrence in occurrences:
        assert occurrence.end_time - occurrence.start_time == datetime.timedelta(hours=48)


@pytest.mark.django_db
def test_bulk_cancel_from_the_day_a_time_happens_twice(
    service: CalendarService, calendar: Calendar
):
    parent = _daily_blocked_time(
        service,
        calendar,
        start=datetime.datetime(2030, 11, 1, 1, 30),
        end=datetime.datetime(2030, 11, 1, 2, 30),
    )

    service.create_recurring_blocked_time_bulk_modification(
        parent_blocked_time=parent,
        modification_start_date=parent.occurrence_start_on(datetime.date(2030, 11, 3)),
        is_bulk_cancelled=True,
    )

    # The 3 November occurrence goes too: it is not before the split.
    assert _starts(_expand_blocked(service, calendar, _utc(11, 1, 0), _utc(11, 6, 0))) == [
        _utc_at(11, 1, 5, 30),
        _utc_at(11, 2, 5, 30),
    ]


@pytest.mark.django_db
def test_bulk_cancel_from_the_day_a_time_does_not_exist(
    service: CalendarService, calendar: Calendar
):
    parent = _daily_blocked_time(
        service,
        calendar,
        start=datetime.datetime(2030, 3, 8, 2, 30),
        end=datetime.datetime(2030, 3, 8, 3, 30),
    )

    service.create_recurring_blocked_time_bulk_modification(
        parent_blocked_time=parent,
        modification_start_date=parent.occurrence_start_on(datetime.date(2030, 3, 10)),
        is_bulk_cancelled=True,
    )

    assert _starts(_expand_blocked(service, calendar, _utc(3, 7, 0), _utc(3, 13, 0))) == [
        _utc_at(3, 8, 7, 30),
        _utc_at(3, 9, 7, 30),
    ]


@pytest.mark.django_db
def test_validator_accepts_the_instant_postgres_uses_on_the_day_a_time_happens_twice(
    service: CalendarService, calendar: Calendar
):
    parent = _daily_blocked_time(
        service,
        calendar,
        start=datetime.datetime(2030, 11, 1, 1, 30),
        end=datetime.datetime(2030, 11, 1, 2, 30),
    )

    assert OccurrenceValidator.validate_modification_date(parent, _utc_at(11, 3, 6, 30))
    assert not OccurrenceValidator.validate_modification_date(parent, _utc_at(11, 3, 5, 30))
    assert not OccurrenceValidator.validate_modification_date(parent, _utc_at(11, 3, 7, 30))


@pytest.mark.django_db
def test_weekly_byday_range_after_the_series_start_across_dst(
    service: CalendarService, calendar: Calendar
):
    _daily_blocked_time(
        service,
        calendar,
        start=datetime.datetime(2030, 3, 4, 9, 0),  # Monday
        end=datetime.datetime(2030, 3, 4, 10, 0),
        rrule_string="FREQ=WEEKLY;BYDAY=MO,WE",
    )

    # After the March change: daylight time, 13:00Z.
    after_spring = _expand_blocked(service, calendar, _utc(3, 8, 0), _utc(3, 21, 0))
    # After the November change: standard time, 14:00Z.
    after_autumn = _expand_blocked(service, calendar, _utc(11, 1, 0), _utc(11, 8, 0))

    assert _starts(after_spring) == [
        _utc(3, 11, 13),
        _utc(3, 13, 13),
        _utc(3, 18, 13),
        _utc(3, 20, 13),
    ]
    assert _starts(after_autumn) == [_utc(11, 4, 14), _utc(11, 6, 14)]


@pytest.mark.django_db
def test_monthly_by_month_day_across_dst(service: CalendarService, calendar: Calendar):
    _daily_blocked_time(
        service,
        calendar,
        start=datetime.datetime(2030, 2, 15, 9, 0),
        end=datetime.datetime(2030, 2, 15, 10, 0),
        rrule_string="FREQ=MONTHLY;BYMONTHDAY=15",
    )

    occurrences = _expand_blocked(service, calendar, _utc(2, 1, 0), _utc(12, 1, 0))

    # 14:00Z in standard time (February, November), 13:00Z in daylight time.
    assert _starts(occurrences) == [
        _utc(2, 15, 14),
        *(_utc(month, 15, 13) for month in range(3, 11)),
        _utc(11, 15, 14),
    ]


@pytest.mark.django_db
def test_master_date_exception_uses_the_local_date_in_the_evening_west_of_utc(
    service: CalendarService, calendar: Calendar
):
    # Daily 21:00 Sao Paulo from 3 June starts at 00:00Z on 4 June, so its UTC date
    # (4 June) is not its local date (3 June). The exception on the master's own
    # date has to be recognised as such.
    parent = _daily_blocked_time(
        service,
        calendar,
        start=datetime.datetime(2030, 6, 3, 21, 0),
        end=datetime.datetime(2030, 6, 3, 22, 0),
        timezone=SAO_PAULO,
    )

    service.create_recurring_blocked_time_exception(
        parent_blocked_time=parent,
        exception_date=datetime.date(2030, 6, 3),
        modified_reason="moved",
    )

    # The master's own date was recognised: a modification there turns the master
    # into a one-off. Taking the UTC date (4 June) instead would have left it
    # recurring, with an exception row for a later occurrence.
    parent.refresh_from_db()
    assert parent.recurrence_rule is None
    assert not BlockedTimeRecurrenceException.objects.unscoped().exists()


@pytest.mark.django_db
def test_modified_occurrence_after_a_dst_change(service: CalendarService, calendar: Calendar):
    parent = _new_york_daily_blocked_time(service, calendar)

    service.create_recurring_blocked_time_exception(
        parent_blocked_time=parent,
        exception_date=datetime.date(2030, 3, 12),
        modified_reason="moved",
        # The modified times are local wall-clock times, like the ones that create a
        # blocked time: 11:00 daylight time is 15:00Z.
        modified_start_time=datetime.datetime(2030, 3, 12, 11, 0),
        modified_end_time=datetime.datetime(2030, 3, 12, 12, 0),
        is_cancelled=False,
    )

    occurrences = _expand_blocked(service, calendar, _utc(3, 11, 0), _utc(3, 14, 0))

    # 12 March keeps one occurrence, the modified one, and not the regular 13:00Z.
    assert _starts(occurrences) == [_utc(3, 11, 13), _utc(3, 12, 15), _utc(3, 13, 13)]


@pytest.mark.django_db
def test_timezone_is_back_to_normal_after_an_error_inside_a_savepoint(
    service: CalendarService, calendar: Calendar
):
    # This only shows that nothing is left behind after the function fails inside a
    # savepoint: rolling the savepoint back also undoes the timezone it set. It does
    # not prove the function restores the timezone on its own, the success-path test
    # above does that.
    parent = _new_york_daily_blocked_time(service, calendar)
    with connection.cursor() as cursor:
        # An interval of 0 makes the function divide by zero after it has switched
        # the timezone.
        cursor.execute("UPDATE calendar_integration_recurrencerule SET interval = 0")

        with pytest.raises(DatabaseError), transaction.atomic():
            cursor.execute(
                "SELECT * FROM calculate_recurring_blocked_times(%s, %s, %s, 10)",
                [parent.id, _utc(3, 9, 0), _utc(3, 12, 0)],
            )

        cursor.execute("SHOW TIME ZONE")
        assert cursor.fetchone() == ("UTC",)


@pytest.fixture
def event_calendar(organization: Organization) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        provider=CalendarProvider.INTERNAL,
        manage_available_windows=False,
        accepts_public_scheduling=True,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("tz_name", [NEW_YORK, SAO_PAULO])
def test_event_bulk_cancel_stores_until_as_the_utc_instant(
    tz_name: str, service: CalendarService, event_calendar: Calendar
):
    # Daily at 21:00 local. The last occurrence before the split is 11 March at
    # 21:00 local, and its UNTIL must be that instant written in UTC. Written as
    # local digits followed by a Z it would read an hour or hours too early.
    parent = service.create_recurring_event(
        event_calendar.id,
        title="Evening",
        description="",
        # Events take instants, unlike blocked times, which take local wall-clock times.
        start_time=local_wall_clock_to_utc(datetime.datetime(2030, 3, 1, 21, 0), tz_name),
        end_time=local_wall_clock_to_utc(datetime.datetime(2030, 3, 1, 22, 0), tz_name),
        timezone=tz_name,
        recurrence_rule="RRULE:FREQ=DAILY",
    )
    split_date = datetime.date(2030, 3, 12)
    last_kept = parent.occurrence_start_on(datetime.date(2030, 3, 11))

    # Truncating the parent runs the ordinary permission-token check, which is not
    # what is under test here. Opened up; every write stays real.
    with patch.object(CalendarPermissionService, "can_perform_update", return_value=True):
        service.create_recurring_event_bulk_modification(
            parent_event=parent,
            modification_start_date=parent.occurrence_start_on(split_date),
            is_bulk_cancelled=True,
        )

    parent = CalendarEvent.objects.filter_by_organization(parent.organization_id).get(id=parent.id)
    assert parent.recurrence_rule is not None
    assert parent.recurrence_rule.until == last_kept
    assert parent.recurrence_rule.to_rrule_string().endswith(
        f"UNTIL={last_kept.strftime('%Y%m%dT%H%M%SZ')}"
    )
    # The occurrence on the UNTIL instant is still there, and nothing after it.
    occurrences = parent.get_occurrences_in_range(_utc(3, 10, 0), _utc(3, 15, 0))
    assert _starts(occurrences)[-1] == last_kept
    assert all(start <= last_kept for start in _starts(occurrences))


def test_rrule_string_writes_an_aware_until_in_utc():
    rule = RecurrenceRule(
        frequency="DAILY",
        until=datetime.datetime(
            2030, 3, 11, 21, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-3))
        ),
    )

    assert rule.to_rrule_string() == "FREQ=DAILY;UNTIL=20300312T000000Z"


def test_truncating_a_rule_at_a_local_time_keeps_the_utc_instant():
    until = datetime.datetime(2030, 3, 11, 21, 0, tzinfo=zoneinfo.ZoneInfo(NEW_YORK))

    truncated = RecurrenceRuleSplitter.truncate_rule_until_date(
        RecurrenceRule(frequency="DAILY"), until
    )

    assert truncated is not None
    assert truncated.until == until
    assert truncated.until.utcoffset() == datetime.timedelta(0)
    assert truncated.to_rrule_string() == "FREQ=DAILY;UNTIL=20300312T010000Z"


@pytest.mark.parametrize(
    ("wall_clock", "expected"),
    [
        # An ordinary time: standard time, UTC-5.
        (datetime.datetime(2030, 3, 1, 9, 0), _utc(3, 1, 14)),
        # Happens twice on 3 November: the later one, in standard time, like Postgres.
        (datetime.datetime(2030, 11, 3, 1, 30), _utc_at(11, 3, 6, 30)),
        # Does not exist on 10 March: the offset from before the change, like Postgres.
        (datetime.datetime(2030, 3, 10, 2, 30), _utc_at(3, 10, 7, 30)),
        # A tzinfo on the input is ignored: only the date and time count.
        (datetime.datetime(2030, 3, 1, 9, 0, tzinfo=datetime.UTC), _utc(3, 1, 14)),
    ],
)
def test_local_wall_clock_to_utc_resolves_dst_edges_like_postgres(
    wall_clock: datetime.datetime, expected: datetime.datetime
):
    assert local_wall_clock_to_utc(wall_clock, NEW_YORK) == expected


# ---------------------------------------------------------------------------
# The same checks over all three row types. The recurrence function is copied once
# for calendar events, once for blocked times and once for available times, so each
# copy needs its own run.
# ---------------------------------------------------------------------------

ALL_KINDS = ["event", "blocked", "available"]


# One of the three row types. They share the recurrence mixin, but each service method
# wants its own type, so the harness works with ``Any``.
Row = Any


class SeriesHarness:
    """Create, change and expand a recurring series of one row type.

    Times that go in are local wall-clock times in ``zone``, whatever the row type
    wants underneath, so a test reads the same for all three.
    """

    def __init__(
        self,
        kind: str,
        service: CalendarService,
        calendar: Calendar,
        organization: Organization,
    ) -> None:
        self.kind = kind
        self.service = service
        self.calendar = calendar
        self.organization = organization

    def create(
        self,
        start: datetime.datetime,
        end: datetime.datetime,
        rrule: str,
        zone: str = NEW_YORK,
    ) -> Row:
        """Create a series whose first occurrence has the stored wall-clock ``start``."""
        if self.kind == "blocked":
            return self.service.create_blocked_time(
                calendar=self.calendar,
                start_time=start,
                end_time=end,
                timezone=zone,
                rrule_string=rrule,
            )
        if self.kind == "available":
            return self.service.create_available_time(
                calendar=self.calendar,
                start_time=start,
                end_time=end,
                timezone=zone,
                rrule_string=rrule,
            )
        # The event service turns the start into an instant and stores it back as a
        # wall-clock, which moves a time that does not exist. Save the row directly to
        # keep the wall-clock as it is given.
        rule = RecurrenceRule.from_rrule_string(rrule, organization=self.organization)
        rule.save()
        event = baker.make(
            CalendarEvent,
            organization=self.organization,
            calendar=self.calendar,
            timezone=zone,
            start_time_tz_unaware=start,
            end_time_tz_unaware=end,
            recurrence_rule=rule,
        )
        event.refresh_from_db()
        return event

    def _reload(self, master: Row) -> Row:
        model = type(master)
        return model.objects.filter_by_organization(self.organization.id).get(id=master.id)

    def expand(
        self,
        master: Row,
        start: datetime.datetime,
        end: datetime.datetime,
        overlap: bool = False,
    ) -> list[datetime.datetime]:
        """Return the sorted start instants of the series, continuations included.

        With ``overlap``, an occurrence that starts before ``start`` but ends after it
        is returned too, which is what the calendar views ask for.
        """
        fresh = self._reload(master)
        rows = [fresh, *fresh.bulk_modifications.all()]
        occurrences: list[Row] = []
        for row in rows:
            occurrences.extend(row.get_occurrences_in_range(start, end, overlap=overlap))
        return _starts(occurrences)

    def _as_input(self, wall_clock: datetime.datetime, zone: str) -> datetime.datetime:
        # Events take instants, blocked and available times take local wall-clock times.
        if self.kind == "event":
            return local_wall_clock_to_utc(wall_clock, zone)
        return wall_clock

    def cancel(self, master: Row, day: datetime.date) -> None:
        """Cancel the occurrence on the local date ``day``."""
        if self.kind == "blocked":
            self.service.create_recurring_blocked_time_exception(
                parent_blocked_time=master,
                exception_date=day,
                is_cancelled=True,
            )
        elif self.kind == "available":
            self.service.create_recurring_available_time_exception(
                parent_available_time=master,
                exception_date=day,
                is_cancelled=True,
            )
        else:
            self.service.create_recurring_event_exception(
                parent_event=master,
                exception_date=day,
                is_cancelled=True,
            )

    def modify(
        self,
        master: Row,
        day: datetime.date,
        start: datetime.datetime,
        end: datetime.datetime,
    ) -> None:
        """Move the occurrence on the local date ``day`` to the wall-clock ``start``."""
        modified_start = self._as_input(start, master.timezone)
        modified_end = self._as_input(end, master.timezone)
        if self.kind == "blocked":
            self.service.create_recurring_blocked_time_exception(
                parent_blocked_time=master,
                exception_date=day,
                modified_start_time=modified_start,
                modified_end_time=modified_end,
            )
        elif self.kind == "available":
            self.service.create_recurring_available_time_exception(
                parent_available_time=master,
                exception_date=day,
                modified_start_time=modified_start,
                modified_end_time=modified_end,
            )
        else:
            self.service.create_recurring_event_exception(
                parent_event=master,
                exception_date=day,
                modified_start_time=modified_start,
                modified_end_time=modified_end,
            )

    def bulk(self, master: Row, day: datetime.date, cancel: bool = False) -> Row | None:
        """Cancel (or, with no changes, split) the series from the local date ``day``."""
        fresh = self._reload(master)
        from_instant = fresh.occurrence_start_on(day)
        if self.kind == "blocked":
            return self.service.create_recurring_blocked_time_bulk_modification(
                parent_blocked_time=fresh,
                modification_start_date=from_instant,
                is_bulk_cancelled=cancel,
                modified_start_time_offset=None if cancel else datetime.timedelta(0),
            )
        if self.kind == "available":
            return self.service.create_recurring_available_time_bulk_modification(
                parent_available_time=fresh,
                modification_start_date=from_instant,
                is_bulk_cancelled=cancel,
                modified_start_time_offset=None if cancel else datetime.timedelta(0),
            )
        return self.service.create_recurring_event_bulk_modification(
            parent_event=fresh,
            modification_start_date=from_instant,
            is_bulk_cancelled=cancel,
            modified_start_time_offset=None if cancel else datetime.timedelta(0),
        )


@pytest.fixture
def harness(
    request: pytest.FixtureRequest, service: CalendarService, organization: Organization
) -> Iterable[SeriesHarness]:
    kind: str = request.param
    # Only one of the two calendar fixtures is built, because two internal calendars of
    # one organization collide on their empty external id.
    calendar: Calendar = request.getfixturevalue(
        "event_calendar" if kind == "event" else "calendar"
    )
    # Truncating an event runs the ordinary permission check, which is not what is
    # under test here. It is opened up for events; every write stays real.
    with patch.object(CalendarPermissionService, "can_perform_update", return_value=True):
        yield SeriesHarness(kind, service, calendar, organization)


by_kind = pytest.mark.parametrize("harness", ALL_KINDS, indirect=True)


def _wall(month: int, day: int, hour: int, minute: int = 0, year: int = 2030) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute)


def _z(month: int, day: int, hour: int, minute: int = 0, year: int = 2030) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC)


@pytest.mark.django_db
@by_kind
def test_series_at_a_time_that_does_not_exist_returns_to_its_time_the_next_day(
    harness: SeriesHarness,
):
    # 02:30 does not exist on 10 March in New York: the clocks jump from 02:00 to
    # 03:00. Postgres reads it with the offset from before the change (07:30Z, shown
    # as 03:30 daylight time). The next day the series must be back at 02:30.
    master = harness.create(_wall(3, 8, 2, 30), _wall(3, 8, 3, 30), "FREQ=DAILY")

    expected = [
        _z(3, 8, 7, 30),  # the series' own first occurrence
        _z(3, 9, 7, 30),
        _z(3, 10, 7, 30),  # the day 02:30 does not exist
        _z(3, 11, 6, 30),  # 02:30 daylight time again
        _z(3, 12, 6, 30),
    ]
    assert master.start_time == expected[0]
    assert harness.expand(master, _z(3, 7, 0), _z(3, 13, 0)) == expected
    # A range that starts after the gap skips ahead instead of stepping through it.
    assert harness.expand(master, _z(3, 11, 0), _z(3, 13, 0)) == expected[3:]


@pytest.mark.django_db
@by_kind
def test_series_that_starts_at_a_time_that_does_not_exist_keeps_its_stored_time(
    harness: SeriesHarness,
):
    # The first occurrence is stored as 02:30 on 10 March, a time that does not exist.
    # Its instant (07:30Z) reads as 03:30 daylight time, but the series is still a
    # 02:30 series: every later day is at 02:30 (06:30Z).
    master = harness.create(_wall(3, 10, 2, 30), _wall(3, 10, 3, 30), "FREQ=DAILY")

    assert master.start_time == _z(3, 10, 7, 30)
    assert harness.expand(master, _z(3, 10, 0), _z(3, 13, 0)) == [
        _z(3, 10, 7, 30),
        _z(3, 11, 6, 30),
        _z(3, 12, 6, 30),
    ]
    # The validator and Python's own day arithmetic agree with Postgres.
    assert master.occurrence_start_on(datetime.date(2030, 3, 10)) == _z(3, 10, 7, 30)
    assert master.occurrence_start_on(datetime.date(2030, 3, 11)) == _z(3, 11, 6, 30)
    assert OccurrenceValidator.validate_modification_date(master, _z(3, 11, 6, 30))
    assert not OccurrenceValidator.validate_modification_date(master, _z(3, 11, 7, 30))

    harness.cancel(master, datetime.date(2030, 3, 12))
    assert harness.expand(master, _z(3, 10, 0), _z(3, 13, 0)) == [
        _z(3, 10, 7, 30),
        _z(3, 11, 6, 30),
    ]


@pytest.mark.django_db
# Not for events: ``update_event``, which truncates the master, turns the start into an
# instant and stores it back as a wall-clock, so an event cannot keep a stored time
# that does not exist. (A series made by the event service never has one.)
@pytest.mark.parametrize("harness", ["blocked", "available"], indirect=True)
def test_bulk_cancel_of_a_series_that_starts_at_a_time_that_does_not_exist(
    harness: SeriesHarness,
):
    master = harness.create(_wall(3, 10, 2, 30), _wall(3, 10, 3, 30), "FREQ=DAILY")

    harness.bulk(master, datetime.date(2030, 3, 12), cancel=True)

    assert harness.expand(master, _z(3, 10, 0), _z(3, 14, 0)) == [
        _z(3, 10, 7, 30),
        _z(3, 11, 6, 30),
    ]


@pytest.mark.django_db
@by_kind
def test_cancelling_the_occurrence_at_a_time_that_happens_twice(harness: SeriesHarness):
    # 01:30 happens twice on 3 November in New York. Postgres picks the later one,
    # 06:30Z. The cancellation has to name that same instant to remove it.
    master = harness.create(_wall(11, 1, 1, 30), _wall(11, 1, 2, 30), "FREQ=DAILY")

    harness.cancel(master, datetime.date(2030, 11, 3))

    assert harness.expand(master, _z(11, 1, 0), _z(11, 6, 0)) == [
        _z(11, 1, 5, 30),
        _z(11, 2, 5, 30),
        _z(11, 4, 6, 30),
        _z(11, 5, 6, 30),
    ]


@pytest.mark.django_db
@by_kind
def test_modified_occurrence_on_the_day_a_time_does_not_exist(harness: SeriesHarness):
    master = harness.create(_wall(3, 8, 2, 30), _wall(3, 8, 3, 30), "FREQ=DAILY")

    harness.modify(master, datetime.date(2030, 3, 10), _wall(3, 10, 11), _wall(3, 10, 12))

    # 11:00 daylight time is 15:00Z. The regular 07:30Z occurrence of that day is gone.
    assert harness.expand(master, _z(3, 8, 0), _z(3, 12, 0)) == [
        _z(3, 8, 7, 30),
        _z(3, 9, 7, 30),
        _z(3, 10, 15),
        _z(3, 11, 6, 30),
    ]


@pytest.mark.django_db
@by_kind
def test_modified_occurrence_on_the_day_a_time_happens_twice(harness: SeriesHarness):
    master = harness.create(_wall(11, 1, 1, 30), _wall(11, 1, 2, 30), "FREQ=DAILY")

    harness.modify(master, datetime.date(2030, 11, 3), _wall(11, 3, 11), _wall(11, 3, 12))

    # 11:00 standard time is 16:00Z. The regular 06:30Z occurrence of that day is gone.
    assert harness.expand(master, _z(11, 1, 0), _z(11, 5, 0)) == [
        _z(11, 1, 5, 30),
        _z(11, 2, 5, 30),
        _z(11, 3, 16),
        _z(11, 4, 6, 30),
    ]


@pytest.mark.django_db
@by_kind
def test_weekly_count_is_not_cut_short_by_a_range_that_starts_just_before_an_occurrence(
    harness: SeriesHarness,
):
    # Weekly on Monday 09:00 New York, six times: 14, 21, 28 October and 4, 11, 18
    # November. The clocks go back on 3 November, so 4 November 09:00 is 14:00Z, an
    # hour later than the earlier Mondays. The occurrences are an hour long and the
    # expansion looks back one duration, so a range that starts at 14:30Z searches
    # from 13:30Z: 30 minutes before the 4 November occurrence. Measured in instants,
    # that counted the occurrence as already passed, and the series ended one
    # occurrence early.
    master = harness.create(_wall(10, 14, 9), _wall(10, 14, 10), "FREQ=WEEKLY;COUNT=6")

    everything = harness.expand(master, _z(10, 1, 0), _z(12, 31, 0))
    from_just_before = harness.expand(master, _z(11, 4, 14, 30), _z(12, 31, 0), overlap=True)

    assert len(everything) == 6
    assert from_just_before == [_z(11, 4, 14), _z(11, 11, 14), _z(11, 18, 14)]


@pytest.mark.django_db
@by_kind
@pytest.mark.parametrize(
    ("first_day", "count", "range_start", "expected"),
    [
        # A series from before the gap. The range starts after it.
        (8, 6, _z(3, 11, 0), [_z(3, 11, 6, 30), _z(3, 12, 6, 30), _z(3, 13, 6, 30)]),
        # A series that starts on the gap day itself.
        (10, 4, _z(3, 11, 0), [_z(3, 11, 6, 30), _z(3, 12, 6, 30), _z(3, 13, 6, 30)]),
        # A range that starts on the gap day, after the series' own first occurrence.
        (10, 4, _z(3, 10, 8), [_z(3, 11, 6, 30), _z(3, 12, 6, 30), _z(3, 13, 6, 30)]),
    ],
)
def test_daily_count_series_at_02_30_with_a_range_after_the_gap_day(
    harness: SeriesHarness,
    first_day: int,
    count: int,
    range_start: datetime.datetime,
    expected: list[datetime.datetime],
):
    master = harness.create(
        _wall(3, first_day, 2, 30), _wall(3, first_day, 3, 30), f"FREQ=DAILY;COUNT={count}"
    )

    assert harness.expand(master, range_start, _z(3, 31, 0)) == expected
    # The whole series still has exactly ``count`` occurrences.
    assert len(harness.expand(master, _z(3, 1, 0), _z(3, 31, 0))) == count


@pytest.mark.django_db
@by_kind
@pytest.mark.parametrize("first_day", [3, 10])
def test_weekly_by_day_series_at_02_30_across_the_gap_day(harness: SeriesHarness, first_day: int):
    # 3 and 10 March 2030 are Sundays, and 10 March is the day 02:30 does not exist.
    master = harness.create(
        _wall(3, first_day, 2, 30), _wall(3, first_day, 3, 30), "FREQ=WEEKLY;BYDAY=SU"
    )

    everything = harness.expand(master, _z(3, 1, 0), _z(3, 26, 0))
    after_the_gap = harness.expand(master, _z(3, 12, 0), _z(3, 26, 0))

    # 3 March is standard time (07:30Z). 10 March is the gap, which reads 07:30Z too.
    expected = [_z(3, 10, 7, 30), _z(3, 17, 6, 30), _z(3, 24, 6, 30)]
    if first_day == 3:
        expected.insert(0, _z(3, 3, 7, 30))
    assert everything == expected
    assert after_the_gap == [_z(3, 17, 6, 30), _z(3, 24, 6, 30)]


@pytest.mark.django_db
@by_kind
@pytest.mark.parametrize("first_month", [1, 3])
def test_monthly_by_month_day_series_at_02_30_across_the_gap_day(
    harness: SeriesHarness, first_month: int
):
    master = harness.create(
        _wall(first_month, 10, 2, 30),
        _wall(first_month, 10, 3, 30),
        "FREQ=MONTHLY;BYMONTHDAY=10",
    )

    # 02:30 local is 07:30Z in standard time and 06:30Z in daylight time. On 10 March
    # itself the time does not exist and reads as 07:30Z.
    standard = {1: _z(1, 10, 7, 30), 2: _z(2, 10, 7, 30), 3: _z(3, 10, 7, 30)}
    daylight = [_z(month, 10, 6, 30) for month in range(4, 11)]
    everything = harness.expand(master, _z(1, 1, 0), _z(11, 1, 0))
    assert everything == [standard[m] for m in range(first_month, 4)] + daylight
    # A range after the gap steps there from the series' start.
    assert harness.expand(master, _z(3, 15, 0), _z(11, 1, 0)) == daylight


@pytest.mark.django_db
@by_kind
def test_yearly_series_keeps_its_local_time_across_dst(harness: SeriesHarness):
    # 10 March 09:00 New York: standard time in 2029 (14:00Z), daylight time in 2030
    # and 2031 (13:00Z).
    master = harness.create(_wall(3, 10, 9, year=2029), _wall(3, 10, 10, year=2029), "FREQ=YEARLY")

    assert harness.expand(master, _z(1, 1, 0, year=2029), _z(12, 31, 0, year=2031)) == [
        _z(3, 10, 14, year=2029),
        _z(3, 10, 13, year=2030),
        _z(3, 10, 13, year=2031),
    ]
    assert harness.expand(master, _z(1, 1, 0, year=2030), _z(12, 31, 0, year=2031)) == [
        _z(3, 10, 13, year=2030),
        _z(3, 10, 13, year=2031),
    ]


@pytest.mark.django_db
@by_kind
@pytest.mark.parametrize(
    ("split_day", "remaining"),
    [
        (3, 4),  # the day 01:30 happens twice: 1 and 2 November are used
        (4, 3),  # the day after: 1, 2 and 3 November are used
    ],
)
def test_continuation_count_across_the_day_a_time_happens_twice(
    harness: SeriesHarness, split_day: int, remaining: int
):
    # Six occurrences, 1 to 6 November, daily at 01:30 New York. 3 November is the day
    # 01:30 happens twice, and the series has it once.
    master = harness.create(_wall(11, 1, 1, 30), _wall(11, 1, 2, 30), "FREQ=DAILY;COUNT=6")

    continuation = harness.bulk(master, datetime.date(2030, 11, split_day))

    assert continuation is not None
    assert continuation.recurrence_rule is not None
    assert continuation.recurrence_rule.count == remaining
    if harness.kind != "event":
        # The blocked and available time services create the continuation from the
        # split instant read as a wall-clock, which moves it by the zone's offset
        # (an existing gap, not caused by this change), so only its count is checked.
        return
    assert harness.expand(master, _z(11, 1, 0), _z(11, 30, 0)) == [
        _z(11, 1, 5, 30),
        _z(11, 2, 5, 30),
        _z(11, 3, 6, 30),
        _z(11, 4, 6, 30),
        _z(11, 5, 6, 30),
        _z(11, 6, 6, 30),
    ]


def test_api_wall_clock_conversion_matches_postgres_on_dst_change_days():
    # Event entry points convert client wall-clock with ``wall_clock_to_utc``. It must
    # resolve the repeated and the skipped hour the same way the generated column does.
    assert wall_clock_to_utc(datetime.datetime(2030, 11, 3, 1, 30), NEW_YORK) == datetime.datetime(
        2030, 11, 3, 6, 30, tzinfo=datetime.UTC
    )
    assert wall_clock_to_utc(datetime.datetime(2030, 3, 10, 2, 30), NEW_YORK) == datetime.datetime(
        2030, 3, 10, 7, 30, tzinfo=datetime.UTC
    )
