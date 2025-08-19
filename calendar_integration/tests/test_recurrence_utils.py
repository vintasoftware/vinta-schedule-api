import datetime

import pytest
from model_bakery import baker

from calendar_integration.constants import RecurrenceFrequency
from calendar_integration.models import CalendarEvent
from calendar_integration.recurrence_utils import (
    create_daily_recurring_event,
    create_monthly_recurring_event,
    create_recurring_event,
    create_weekly_recurring_event,
    get_events_in_range,
)


def _dt(year, month, day, hour=9, minute=0):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC)


@pytest.mark.django_db
def test_create_recurring_event_basic():
    org = baker.make("organizations.Organization")
    cal = baker.make(
        "calendar_integration.Calendar", organization=org, external_id=baker.seq("cal")
    )
    start = _dt(2025, 1, 1)
    end = start + datetime.timedelta(hours=1)
    event = create_recurring_event(
        calendar=cal,
        title="Generic Recurring",
        description="Desc",
        start_time=start,
        end_time=end,
        frequency=RecurrenceFrequency.DAILY,
        interval=2,
        count=5,
        external_id="generic-recurring",
    )
    assert event.id is not None
    assert event.is_recurring is True
    assert event.recurrence_rule.frequency == RecurrenceFrequency.DAILY
    assert event.recurrence_rule.interval == 2
    assert event.recurrence_rule.count == 5


@pytest.mark.django_db
def test_create_daily_recurring_event_convenience():
    org = baker.make("organizations.Organization")
    cal = baker.make(
        "calendar_integration.Calendar", organization=org, external_id=baker.seq("cal")
    )
    start = _dt(2025, 2, 1)
    end = start + datetime.timedelta(minutes=30)
    event = create_daily_recurring_event(
        calendar=cal,
        title="Daily",
        start_time=start,
        end_time=end,
        count=3,
        external_id="daily-convenience",
    )
    assert event.recurrence_rule.frequency == RecurrenceFrequency.DAILY
    assert event.recurrence_rule.interval == 1  # default
    # Generate instances to ensure count respected
    instances = event.generate_instances(start, start + datetime.timedelta(days=10))
    assert len(instances) == 3


@pytest.mark.django_db
def test_create_weekly_recurring_event_with_weekdays():
    org = baker.make("organizations.Organization")
    cal = baker.make(
        "calendar_integration.Calendar", organization=org, external_id=baker.seq("cal")
    )
    start = _dt(2025, 3, 3)  # a Monday
    end = start + datetime.timedelta(hours=1)
    event = create_weekly_recurring_event(
        calendar=cal,
        title="Weekly",
        start_time=start,
        end_time=end,
        weekdays=["MO", "WE", "FR"],
        interval=1,
        count=5,
        external_id="weekly-weekdays",
    )
    assert event.recurrence_rule.frequency == RecurrenceFrequency.WEEKLY
    assert event.recurrence_rule.by_weekday.split(",") == ["MO", "WE", "FR"]
    # Ensure generated instances fall only on specified weekdays
    instances = event.generate_instances(start, start + datetime.timedelta(weeks=3))
    assert all(inst.start_time.weekday() in {0, 2, 4} for inst in instances)


@pytest.mark.django_db
def test_create_monthly_recurring_event():
    org = baker.make("organizations.Organization")
    cal = baker.make(
        "calendar_integration.Calendar", organization=org, external_id=baker.seq("cal")
    )
    start = _dt(2025, 4, 15)
    end = start + datetime.timedelta(minutes=45)
    event = create_monthly_recurring_event(
        calendar=cal,
        title="Monthly",
        start_time=start,
        end_time=end,
        interval=1,
        count=3,
        external_id="monthly-event",
    )
    assert event.recurrence_rule.frequency == RecurrenceFrequency.MONTHLY
    # Generated instances should maintain day of month (15th)
    instances = event.generate_instances(start, _dt(2025, 8, 1))
    assert len(instances) == 3
    assert {inst.start_time.day for inst in instances} == {15}


@pytest.mark.django_db
def test_get_events_in_range_includes_expanded_recurring():
    org = baker.make("organizations.Organization")
    cal = baker.make(
        "calendar_integration.Calendar", organization=org, external_id=baker.seq("cal")
    )
    start = _dt(2025, 5, 1)
    end = start + datetime.timedelta(minutes=30)
    recurring = create_daily_recurring_event(
        calendar=cal,
        title="Daily",
        start_time=start,
        end_time=end,
        count=3,
        external_id="daily-range",
    )
    # Non-recurring event inside range
    baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization=org,
        title="One-off",
        start_time=_dt(2025, 5, 2, 14),
        end_time=_dt(2025, 5, 2, 15),
        external_id="one-off-range",
    )
    events = get_events_in_range(
        calendar=cal,
        start_date=_dt(2025, 5, 1),
        end_date=_dt(2025, 5, 5),
        include_recurring=True,
    )
    # Expect 3 daily instances + the single event
    assert len(events) == 4
    # All sorted ascending
    assert events == sorted(events, key=lambda e: e.start_time)
    # Ensure parent recurring event object itself not included (only instances)
    assert recurring not in events


@pytest.mark.django_db
def test_get_events_in_range_exclude_recurring_instances():
    org = baker.make("organizations.Organization")
    cal = baker.make(
        "calendar_integration.Calendar", organization=org, external_id=baker.seq("cal")
    )
    start = _dt(2025, 6, 1)
    end = start + datetime.timedelta(minutes=30)
    parent = create_daily_recurring_event(
        calendar=cal,
        title="Daily",
        start_time=start,
        end_time=end,
        count=2,
        external_id="daily-exclude",
    )
    events = get_events_in_range(
        calendar=cal,
        start_date=_dt(2025, 6, 1),
        end_date=_dt(2025, 6, 5),
        include_recurring=False,
    )
    # Only the parent recurring event should appear
    assert events == [parent]
