"""
Utility functions for working with recurring calendar events.
"""

import datetime

from .constants import RecurrenceFrequency
from .models import CalendarEvent, RecurrenceRule


def create_recurring_event(
    calendar,
    title: str,
    description: str,
    start_time: datetime.datetime,
    end_time: datetime.datetime,
    frequency: str,
    interval: int = 1,
    count: int | None = None,
    until: datetime.datetime | None = None,
    by_weekday: str | None = None,
    **kwargs,
) -> CalendarEvent:
    """
    Create a recurring calendar event with a recurrence rule.

    Args:
        calendar: Calendar instance
        title: Event title
        description: Event description
        start_time: Event start time
        end_time: Event end time
        frequency: Recurrence frequency (DAILY, WEEKLY, MONTHLY, YEARLY)
        interval: Interval between occurrences (default: 1)
        count: Number of occurrences (optional)
        until: End date for recurrence (optional)
        by_weekday: Comma-separated weekdays for weekly recurrence (e.g., "MO,WE,FR")
        **kwargs: Additional CalendarEvent fields

    Returns:
        CalendarEvent instance with recurrence rule
    """
    # Create the recurrence rule
    recurrence_rule = RecurrenceRule.objects.create(
        organization=calendar.organization,
        frequency=frequency,
        interval=interval,
        count=count,
        until=until,
        by_weekday=by_weekday or "",
    )

    # Create the main event
    event = CalendarEvent.objects.create(
        calendar_fk=calendar,
        organization=calendar.organization,
        title=title,
        description=description,
        start_time=start_time,
        end_time=end_time,
        recurrence_rule_fk=recurrence_rule,
        **kwargs,
    )

    return event


def create_daily_recurring_event(
    calendar,
    title: str,
    start_time: datetime.datetime,
    end_time: datetime.datetime,
    interval: int = 1,
    count: int | None = None,
    until: datetime.datetime | None = None,
    **kwargs,
) -> CalendarEvent:
    """Create a daily recurring event."""
    return create_recurring_event(
        calendar=calendar,
        title=title,
        description=kwargs.get("description", ""),
        start_time=start_time,
        end_time=end_time,
        frequency=RecurrenceFrequency.DAILY,
        interval=interval,
        count=count,
        until=until,
        **kwargs,
    )


def create_weekly_recurring_event(
    calendar,
    title: str,
    start_time: datetime.datetime,
    end_time: datetime.datetime,
    weekdays: list[str] | None = None,
    interval: int = 1,
    count: int | None = None,
    until: datetime.datetime | None = None,
    **kwargs,
) -> CalendarEvent:
    """
    Create a weekly recurring event.

    Args:
        weekdays: List of weekday abbreviations (e.g., ["MO", "WE", "FR"])
    """
    by_weekday = ",".join(weekdays) if weekdays else None
    return create_recurring_event(
        calendar=calendar,
        title=title,
        description=kwargs.get("description", ""),
        start_time=start_time,
        end_time=end_time,
        frequency=RecurrenceFrequency.WEEKLY,
        interval=interval,
        count=count,
        until=until,
        by_weekday=by_weekday,
        **kwargs,
    )


def create_monthly_recurring_event(
    calendar,
    title: str,
    start_time: datetime.datetime,
    end_time: datetime.datetime,
    interval: int = 1,
    count: int | None = None,
    until: datetime.datetime | None = None,
    **kwargs,
) -> CalendarEvent:
    """Create a monthly recurring event."""
    return create_recurring_event(
        calendar=calendar,
        title=title,
        description=kwargs.get("description", ""),
        start_time=start_time,
        end_time=end_time,
        frequency=RecurrenceFrequency.MONTHLY,
        interval=interval,
        count=count,
        until=until,
        **kwargs,
    )


def get_events_in_range(
    calendar,
    start_date: datetime.datetime,
    end_date: datetime.datetime,
    include_recurring: bool = True,
) -> list[CalendarEvent]:
    """
    Get all events (recurring and non-recurring) in a date range for a calendar.

    Args:
        calendar: Calendar instance
        start_date: Start of date range
        end_date: End of date range
        include_recurring: Whether to expand recurring events into instances

    Returns:
        List of CalendarEvent instances
    """
    events = []

    # Get all events that start within the range
    calendar_events = calendar.events.filter(start_time__lte=end_date, end_time__gte=start_date)

    for event in calendar_events:
        if event.is_recurring and include_recurring:
            # Get all occurrences of this recurring event in the range
            occurrences = event.get_occurrences_in_range(start_date, end_date)
            events.extend(occurrences)
        elif not event.is_recurring_instance:  # Avoid duplicating instances
            events.append(event)

    # Sort by start time
    events.sort(key=lambda x: x.start_time)
    return events


# Example usage:
"""
from django.utils import timezone
from calendar_integration.models import Calendar
from calendar_integration.recurrence_utils import (
    create_daily_recurring_event,
    create_weekly_recurring_event,
    get_events_in_range
)

# Assume you have a calendar instance
calendar = Calendar.objects.first()

# Create a daily standup meeting
start_time = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
end_time = start_time + datetime.timedelta(minutes=30)

daily_standup = create_daily_recurring_event(
    calendar=calendar,
    title="Daily Standup",
    start_time=start_time,
    end_time=end_time,
    count=30,  # 30 occurrences
    description="Daily team standup meeting"
)

# Create a weekly team meeting (every Monday and Wednesday)
weekly_meeting = create_weekly_recurring_event(
    calendar=calendar,
    title="Team Meeting",
    start_time=start_time.replace(hour=14, minute=0),  # 2 PM
    end_time=start_time.replace(hour=15, minute=0),    # 3 PM
    weekdays=["MO", "WE"],
    count=20,
    description="Weekly team sync"
)

# Get all events for the next month
next_month_start = timezone.now()
next_month_end = next_month_start + datetime.timedelta(days=30)

all_events = get_events_in_range(
    calendar=calendar,
    start_date=next_month_start,
    end_date=next_month_end,
    include_recurring=True
)

print(f"Found {len(all_events)} events in the next month")

# Create an exception (cancel one occurrence)
exception_date = start_time + datetime.timedelta(days=7)
daily_standup.create_exception(
    exception_date=exception_date,
    is_cancelled=True
)
"""
