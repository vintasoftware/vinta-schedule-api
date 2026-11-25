"""Let ``managed_calendar_is_free_in_range`` ignore named rows.

Version 0002 takes the id of an event and the ids of blocked times that do not count
as busy. Rescheduling an appointment-type event checks the new time on every crew
calendar while the event and its own blocked times still sit at the old time, so it
passes them here to keep a move that overlaps the old slot from conflicting with
itself.

The new version changes the argument list, so it is a new function to Postgres. The
forward SQL drops the 4-argument version first. The reverse drops the 6-argument
version before restoring 0001; without that, both would exist and a 4-argument call
would be ambiguous.
"""

from django.db import migrations

from calendar_integration.migrations.sql.functions.managed_calendar_is_free_in_range import (
    ManagedCalendarIsFreeInRangeMigrationManager,
)


_manager = ManagedCalendarIsFreeInRangeMigrationManager("calendar_integration", "0002")


class Migration(migrations.Migration):
    dependencies = [
        ("calendar_integration", "0071_calendar_event_meeting"),
    ]

    operations = [
        migrations.RunSQL(
            sql=_manager.get_forward_sql(),
            reverse_sql=(
                "DROP FUNCTION IF EXISTS managed_calendar_is_free_in_range("
                "BIGINT, BIGINT, TIMESTAMPTZ, TIMESTAMPTZ, BIGINT, BIGINT[]);\n"
                + _manager.get_backward_sql()
            ),
        ),
    ]
