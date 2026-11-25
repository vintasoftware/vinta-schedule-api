"""Let ``managed_calendar_is_free_in_range`` ignore named rows.

Version 0002 takes the id of an event and the ids of blocked times that do not count
as busy. Rescheduling an appointment-type event checks the new time on every crew
calendar while the event and its own blocked times still sit at the old time, so it
passes them here to keep a move that overlaps the old slot from conflicting with
itself.

The new arguments change the function's signature. The migration manager drops the
4-argument version going forward and the 6-argument one going back.
"""

from django.db import migrations

from calendar_integration.migrations.sql.functions.managed_calendar_is_free_in_range import (
    ManagedCalendarIsFreeInRangeMigrationManager,
)


class Migration(migrations.Migration):
    dependencies = [
        ("calendar_integration", "0071_calendar_event_meeting"),
    ]

    operations = [
        ManagedCalendarIsFreeInRangeMigrationManager("calendar_integration", "0002").migration(),
    ]
