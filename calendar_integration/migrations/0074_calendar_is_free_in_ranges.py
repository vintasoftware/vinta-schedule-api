"""Replace ``managed_calendar_is_free_in_range`` with ``calendar_is_free_in_ranges``.

The new function is the one check of whether a calendar is free, for every kind of
calendar: crew calendars, calendars that manage their availability windows, and rooms,
including rooms held by an event on another calendar. It takes many ranges at once, so
a recurring series is checked in one call, and arrays of ignored event and blocked
time ids. ``CalendarQuerySet.only_calendars_available_in_ranges`` calls it for every
calendar, so the old function, which only covered calendars that manage windows, is
dropped. Going back creates it again.
"""

from django.db import migrations

from calendar_integration.migrations.sql.functions.calendar_is_free_in_ranges import (
    CalendarIsFreeInRangesMigrationManager,
)
from calendar_integration.migrations.sql.functions.managed_calendar_is_free_in_range import (
    ManagedCalendarIsFreeInRangeMigrationManager,
)


class Migration(migrations.Migration):
    dependencies = [
        ("calendar_integration", "0073_event_and_blocked_time_ical_uid"),
    ]

    operations = [
        CalendarIsFreeInRangesMigrationManager("calendar_integration", "0001").migration(),
        ManagedCalendarIsFreeInRangeMigrationManager("calendar_integration", "0002").retirement(),
    ]
