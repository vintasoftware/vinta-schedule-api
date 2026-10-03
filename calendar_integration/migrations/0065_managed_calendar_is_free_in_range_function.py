"""Install ``managed_calendar_is_free_in_range``.

``CalendarQuerySet.only_calendars_available_in_ranges`` calls it for calendars that
manage their own availability windows. Before this, that branch matched only the
master ``AvailableTime`` row's own start/end, so a recurring window counted on its
first date and never again, and it never looked at events or blocked times.
"""

from django.db import migrations

from calendar_integration.migrations.sql.functions.managed_calendar_is_free_in_range import (
    ManagedCalendarIsFreeInRangeMigrationManager,
)


class Migration(migrations.Migration):
    dependencies = [
        ("calendar_integration", "0064_calendarevent_external_id_nullable"),
    ]

    operations = [
        ManagedCalendarIsFreeInRangeMigrationManager("calendar_integration", "0001").migration(),
    ]
