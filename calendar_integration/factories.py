import datetime

from .constants import RecurrenceFrequency
from .models import CalendarEvent, RecurrenceRule


class CalendarEventFactory:
    @staticmethod
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
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            timezone=kwargs.get("timezone", "UTC"),
            recurrence_rule_fk=recurrence_rule,
            **kwargs,
        )

        return event

    @classmethod
    def create_daily_recurring_event(
        cls,
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
        return cls.create_recurring_event(
            calendar=calendar,
            title=title,
            description=kwargs.get("description", ""),
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            frequency=RecurrenceFrequency.DAILY,
            interval=interval,
            count=count,
            until=until,
            **kwargs,
        )

    @classmethod
    def create_weekly_recurring_event(
        cls,
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
        return cls.create_recurring_event(
            calendar=calendar,
            title=title,
            description=kwargs.get("description", ""),
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            frequency=RecurrenceFrequency.WEEKLY,
            interval=interval,
            count=count,
            until=until,
            by_weekday=by_weekday,
            **kwargs,
        )

    @classmethod
    def create_monthly_recurring_event(
        cls,
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
        return cls.create_recurring_event(
            calendar=calendar,
            title=title,
            description=kwargs.get("description", ""),
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            frequency=RecurrenceFrequency.MONTHLY,
            interval=interval,
            count=count,
            until=until,
            **kwargs,
        )

    @classmethod
    def get_events_in_range(
        cls,
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
        calendar_events = calendar.events.annotate_recurring_occurrences_on_date_range().filter(
            start_time__lte=end_date, end_time__gte=start_date
        )

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
